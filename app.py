import streamlit as st
import pandas as pd
from datetime import datetime, timedelta
import unicodedata
from bs4 import BeautifulSoup

import os
import json

import time
import io
import requests
import math

# --- LIBRERIE NBA API ---
from nba_api.stats.static import players, teams
from nba_api.stats.endpoints import playergamelog, commonplayerinfo, playerdashboardbygeneralsplits, leaguedashplayerstats, scoreboardv3

# --- LIBRERIE SELENIUM ---
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from webdriver_manager.chrome import ChromeDriverManager

# =====================================================================
# 1. FUNZIONI MATEMATICHE E SCRAPER
# =====================================================================

DEF_THRESHOLDS = {"PG": 110.77, "SG": 110.86, "SF": 110.01, "PF": 108.87, "C": 106.49}

def calc_prob_over_10(mu, sigma):
    """Calcola la probabilità di fare 10+ usando la distribuzione normale."""
    if pd.isna(sigma) or sigma == 0:
        return 1.0 if mu >= 10 else 0.0
    z = (9.5 - mu) / (sigma * math.sqrt(2.0))
    return (1.0 - math.erf(z)) / 2.0

def calc_double_double_prob(res, stds):
    """Applica il teorema delle probabilità congiunte per la Doppia Doppia."""
    p_pts = calc_prob_over_10(res['PTS'], stds['PTS'])
    p_reb = calc_prob_over_10(res['REB'], stds['REB'])
    p_ast = calc_prob_over_10(res['AST'], stds['AST'])
    
    p_dd = (p_pts * p_reb) + (p_pts * p_ast) + (p_reb * p_ast) - (2 * p_pts * p_reb * p_ast)
    return p_dd

def calc_triple_double_prob(res, stds):
    """Applica il teorema delle probabilità congiunte per la Tripla Doppia."""
    p_pts = calc_prob_over_10(res['PTS'], stds['PTS'])
    p_reb = calc_prob_over_10(res['REB'], stds['REB'])
    p_ast = calc_prob_over_10(res['AST'], stds['AST'])
    
    # Probabilità che si verifichino tutte e 3 contemporaneamente
    return p_pts * p_reb * p_ast

def normalize_name(name):
    return unicodedata.normalize('NFD', name).encode('ascii', 'ignore').decode('utf-8').lower()

def clean_name_for_match(name):
    n = normalize_name(name).replace(".", "").replace("-", " ")
    for suffix in [" jr", " sr", " ii", " iii", " iv"]:
        if n.endswith(suffix):
            n = n[:-len(suffix)]
    return n.strip()

def safe_api_call(endpoint_class, **kwargs):
    tentativi = 5
    attesa = 2.0  # Partiamo con 2 secondi di attesa
    
    for i in range(tentativi):
        try:
            response = endpoint_class(**kwargs)
            return response.get_data_frames()[0]
        except Exception as e:
            if i < tentativi - 1:
                print(f"⚠️ Server NBA non risponde. Ripietiamo tra {attesa} secondi... (Tentativo {i+1}/{tentativi})")
                time.sleep(attesa)
                attesa *= 2  # Raddoppia il tempo di attesa ad ogni fallimento (2s -> 4s -> 8s -> 16s)
            else:
                print(f"❌ Fallimento definitivo dopo {tentativi} tentativi per l'endpoint {endpoint_class.__name__}.")
    return pd.DataFrame()

def get_injury_stats(name, season):
    all_p = players.get_active_players()
    p_dict = next((p for p in all_p if normalize_name(p['full_name']) == normalize_name(name)), None)
    if not p_dict: return None
    try:
        info = safe_api_call(commonplayerinfo.CommonPlayerInfo, player_id=p_dict['id'])
        dash_b = safe_api_call(playerdashboardbygeneralsplits.PlayerDashboardByGeneralSplits, player_id=p_dict['id'], season=season, measure_type_detailed='Base', per_mode_detailed='PerGame')
        dash_a = safe_api_call(playerdashboardbygeneralsplits.PlayerDashboardByGeneralSplits, player_id=p_dict['id'], season=season, measure_type_detailed='Advanced', per_mode_detailed='PerGame')
        return {"name": name, "pos": info['POSITION'].values[0], "mpg": dash_b['MIN'].values[0], "ppg": dash_b['PTS'].values[0], "defrtg": dash_a['DEF_RATING'].values[0]}
    except: return None

def are_positions_similar(pos1, pos2):
    p1, p2 = pos1.upper(), pos2.upper()
    similar_groups = [{"PG", "SG"}, {"SG", "SF"}, {"SF", "PF"}, {"PF", "C"}]
    if p1 == p2: return True
    for group in similar_groups:
        if p1 in group and p2 in group: return True
    return False

def get_espn_injuries(team_name):
    url = "https://www.espn.com/nba/injuries"
    headers = {'User-Agent': 'Mozilla/5.0'}
    injuries = {'out': [], 'dtd': []}
    
    try:
        response = requests.get(url, headers=headers, timeout=10)
        soup = BeautifulSoup(response.text, 'html.parser')
        containers = soup.find_all('div', class_='ResponsiveTable')
        
        for container in containers:
            title_span = container.find('span', class_='injuries__teamName')
            if title_span and team_name.lower() in title_span.text.lower():
                table = container.find('table')
                if table:
                    rows = table.find_all('tr')
                    for row in rows[1:]:
                        cols = row.find_all('td')
                        if len(cols) >= 4:
                            player_name = cols[0].text.strip()
                            status = cols[3].text.strip()
                            if 'Day-To-Day' in status or 'DTD' in status:
                                injuries['dtd'].append(player_name)
                            else:
                                injuries['out'].append(player_name)
                break
    except Exception as e:
        print(f"Errore scraping infortuni: {e}")
    return injuries

def fetch_dunksandthrees_def(injured_players, opp_abb):
    if not injured_players: return {}
    options = Options()
    options.add_argument("--headless")
    options.add_argument("--window-size=1920,1080")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    
    # --- LA MODIFICA È QUI ---
    options.binary_location = "/usr/bin/chromium"
    service = Service("/usr/bin/chromedriver")
    driver = webdriver.Chrome(service=service, options=options)
    # -------------------------
    
    def_dict = {}
    nomi_da_cercare = injured_players.copy()
    
    try:
        url = f"https://dunksandthrees.com/epm?m=def&team={opp_abb}"
        driver.get(url)
        time.sleep(4) 
        soup = BeautifulSoup(driver.page_source, 'html.parser')
        rows = soup.find_all('tr')
        
        for row in rows:
            cells = row.find_all('td')
            if len(cells) >= 5:
                player_link = cells[0].find('a', href=lambda h: h and '/player/' in h)
                if not player_link: continue 
                raw_name_cell = player_link.text.strip() 
                for target_name in list(nomi_da_cercare):
                    target_clean = clean_name_for_match(target_name)
                    cell_clean = clean_name_for_match(raw_name_cell)
                    if target_clean in cell_clean or cell_clean in target_clean:
                        try:
                            def_div = cells[4].find('div', class_='text-foreground')
                            if def_div:
                                def_val = float(def_div.text.strip().replace('+', ''))
                                if normalize_name(target_name) not in def_dict:
                                    def_dict[normalize_name(target_name)] = def_val
                                    nomi_da_cercare.remove(target_name)
                        except: pass
    except: pass
    finally: driver.quit()
    return def_dict

def get_espn_starters(team_abb):
    espn_map = {'GSW': 'GS', 'NOP': 'NO', 'NYK': 'NY', 'SAS': 'SA', 'UTA': 'UTAH', 'WAS': 'WSH'}
    abb = espn_map.get(team_abb.upper(), team_abb.upper()).lower()
    url = f"https://www.espn.com/nba/team/depth/_/name/{abb}"
    headers = {'User-Agent': 'Mozilla/5.0'}
    
    starters = []
    try:
        response = requests.get(url, headers=headers, timeout=10)
        soup = BeautifulSoup(response.text, 'html.parser')
        tables = soup.find_all('table')
        if len(tables) >= 2:
            rows = tables[1].find_all('tr')
            for row in rows:
                cells = row.find_all('td')
                if cells:
                    a_tag = cells[0].find('a')
                    if a_tag:
                        starters.append(a_tag.text.strip())
        return list(dict.fromkeys(starters))[:5]
    except: return []

# --- FUNZIONE AGGIORNATA PER LA NUOVA LOGICA INFORTUNI ---
def evaluate_injury_bonus(infortuni_tua, infortuni_avv, target_pos, current_season, def_data, compagni_da_ignorare=None):
    if compagni_da_ignorare is None: compagni_da_ignorare = []
    bonus_pra_assoluto = 0.0
    bonus_perc_difesa = 0.0
    
    # Infortuni di squadra (Volume offensivo extra -> Valore Assoluto in PRA)
    for p in infortuni_tua:
        if normalize_name(p) in compagni_da_ignorare: continue
        stats = get_injury_stats(p, current_season)
        if stats and stats['mpg'] >= 20 and stats['ppg'] > 10:
            val = 1.0 if are_positions_similar(target_pos, stats['pos']) else 0.6
            bonus_pra_assoluto += val
            
    # Infortuni avversari (Efficienza offensiva extra -> Valore Percentuale)
    for p in infortuni_avv:
        stats = get_injury_stats(p, current_season)
        if stats and stats['mpg'] >= 20:
            p_norm = normalize_name(stats['name'])
            def_stat = def_data.get(p_norm, -99.9) 
            pos_avv = stats['pos'].upper()
            soglia_def = 1.0 if any(r in pos_avv for r in ['PF', 'C']) else 0.5
            if def_stat >= soglia_def:
                # 5% se stesso ruolo, 3% se ruoli diversi
                val = 0.05 if are_positions_similar(target_pos, stats['pos']) else 0.03
                bonus_perc_difesa += val
                
    return bonus_pra_assoluto, bonus_perc_difesa

def fetch_dvp_rankings(pos):
    url = "https://www.fantasypros.com/daily-fantasy/nba/fanduel-defense-vs-position.php"
    chrome_options = Options()
    chrome_options.add_argument("--headless")
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--disable-dev-shm-usage")
    chrome_options.add_argument("--disable-gpu") # Utile sui server senza schermo
    
    # --- LA MODIFICA È QUI ---
    # Diciamo a Python dove trovare esattamente browser e driver su Streamlit Cloud
    chrome_options.binary_location = "/usr/bin/chromium"
    service = Service("/usr/bin/chromedriver")
    driver = webdriver.Chrome(service=service, options=chrome_options)
    # -------------------------
    
    try:
        driver.get(url)
        if pos.upper() != "ALL":
            try:
                tab = WebDriverWait(driver, 5).until(EC.element_to_be_clickable((By.XPATH, f"//*[text()='{pos.upper()}']")))
                driver.execute_script("arguments[0].click();", tab)
                time.sleep(2)
            except: pass
        df = pd.read_html(io.StringIO(driver.page_source))[0]
        for c in ['PTS', 'REB', 'AST']:
            df[f'{c}_Rank'] = df[c].rank(ascending=False, method='min').astype(int)
        return df
    finally: driver.quit()

def calculate_weighted_stat(stat_name, df_f, df_h, df_s, rank_dvp, rientro):
    m_f = df_f[stat_name].mean()
    m_h = df_h[stat_name].mean() if len(df_h) > 0 else m_f
    m_s = df_s[stat_name].mean()
    
    w_h, w_f, w_s = 0.50, 0.25, 0.25
    if len(df_h) < 6:
        w_h, w_f, w_s = w_h - 0.30, w_f + 0.15, w_s + 0.15
    if rientro:
        w_f, w_h, w_s = w_f - 0.15, w_h + 0.05, w_s + 0.10
        
    base_player = ((m_f * w_f) + (m_h * w_h) + (m_s * w_s)) / (w_f + w_h + w_s)
    
    dvp_modifier = 1.0
    if rank_dvp <= 10:
        dvp_modifier = 1.05 
    elif rank_dvp >= 21:
        dvp_modifier = 0.95 
        
    return max(0.1, base_player * dvp_modifier)

def generate_strategic_advice(res, df_s, ranks):
    baseline = {"PTS": df_s['PTS'].mean(), "REB": df_s['REB'].mean(), "AST": df_s['AST'].mean()}
    scores = {}
    for s in ["PTS", "REB", "AST"]:
        dvp_bonus = 1.2 if ranks[s] <= 5 else 1.1 if ranks[s] <= 10 else 1.0
        scores[s] = (res[s] / (baseline[s] if baseline[s] > 0 else 1)) * dvp_bonus
    sorted_scores = sorted(scores.items(), key=lambda x: x[1], reverse=True)
    best_s = sorted_scores[0][0]
    if res[best_s] < 4 and best_s != "PTS": return f"COMBO {best_s} + {sorted_scores[1][0]}", [best_s, sorted_scores[1][0]]
    if sorted_scores[0][1] - sorted_scores[1][1] < 0.15: return f"COMBO {best_s} + {sorted_scores[1][0]}", [best_s, sorted_scores[1][0]]
    return f"{best_s} (Singola)", [best_s]


#-------Funzioni per scrittura dati in locale

#-------Funzioni per scrittura dati IN CLOUD (JSONBin)

# INSERISCI QUI I TUOI DATI DI JSONBIN.IO
BIN_ID = "69aaf659d0ea881f40f5c8fc"
MASTER_KEY = "$2a$10$KVcu.YukwCvU2CRsR.V2F.8SyEKlaA3JxMOmDSBTHBGif/wtZ3.UK"

JSONBIN_URL = f"https://api.jsonbin.io/v3/b/{BIN_ID}"
HEADERS = {
    "X-Master-Key": MASTER_KEY,
    "Content-Type": "application/json"
}

def carica_e_pulisci_database():
    """Carica il JSON dal Cloud ed elimina i giocatori analizzati da più di 3 giorni."""
    try:
        risposta = requests.get(JSONBIN_URL, headers=HEADERS)
        # JSONBin inserisce i nostri dati dentro una chiave chiamata "record"
        dati = risposta.json().get("record", {})
    except Exception as e:
        print(f"Errore caricamento database cloud: {e}")
        return {}

    dati_validi = {}
    ora_attuale = datetime.now()
    
    for giocatore, info in dati.items():
        if "timestamp" in info:
            data_creazione = datetime.fromisoformat(info["timestamp"])
            # Pulizia sicura ignorando il fuso orario
            if (ora_attuale.replace(tzinfo=None) - data_creazione.replace(tzinfo=None)).days < 3:
                dati_validi[giocatore] = info
                
    # Se il database online aveva roba vecchia, lo sovrascriviamo subito con la versione pulita
    if len(dati_validi) != len(dati):
        requests.put(JSONBIN_URL, json=dati_validi, headers=HEADERS)
        
    return dati_validi

def salva_nel_database(dati_aggiornati):
    """Salva l'intero dizionario delle proiezioni sovrascrivendo il file in Cloud."""
    try:
        requests.put(JSONBIN_URL, json=dati_aggiornati, headers=HEADERS)
    except Exception as e:
        print(f"Errore salvataggio database cloud: {e}")

# =====================================================================
# 2. INTERFACCIA GRAFICA STREAMLIT
# =====================================================================

st.set_page_config(page_title="NBA Multi-Scanner Pro", page_icon="🏀", layout="wide")

if 'proiezioni_giocatori' not in st.session_state: 
    # Carica le proiezioni vecchie (ma valide) dal file locale
    st.session_state.proiezioni_giocatori = carica_e_pulisci_database()
if 'partite_oggi' not in st.session_state: st.session_state.partite_oggi = []
if 'team_dict' not in st.session_state: st.session_state.team_dict = {}
if 'team_name_dict' not in st.session_state: st.session_state.team_name_dict = {}
if 'starters_list' not in st.session_state: st.session_state.starters_list = []
if 'infortunati_disponibili' not in st.session_state: st.session_state.infortunati_disponibili = []

st.sidebar.title("🏀 NBA Scanner Pro")
menu = st.sidebar.radio("Menu Principale", ["1. 🔍 Analisi Partita", "2. 📊 Valutatore Quote (EV)"])
st.sidebar.markdown("---")
st.sidebar.info("I giocatori analizzati resteranno in memoria finché non chiudi la finestra.")

# ---------------------------------------------------------------------
# PAGINA 1: ANALISI PARTITA E GIOCATORI
# ---------------------------------------------------------------------
if menu == "1. 🔍 Analisi Partita":
    st.title("Impostazione Match e Giocatori")
    
    if st.button("🔄 Cerca Partite di Oggi"):
        with st.spinner("Scaricamento tabellone ufficiale NBA in corso..."):
            nba_t = teams.get_teams()
            st.session_state.team_dict = {t['id']: t['abbreviation'] for t in nba_t}
            st.session_state.team_name_dict = {t['abbreviation']: t['full_name'] for t in nba_t}
            
            partite_oggi = []
            try:
                data_oggi = datetime.now().strftime('%Y-%m-%d')
                board_data = scoreboardv3.ScoreboardV3(game_date=data_oggi).get_dict()
                games = board_data.get('scoreboard', {}).get('games', [])
                for game in games:
                    away_abb = game.get('awayTeam', {}).get('teamTricode', 'UNK')
                    home_abb = game.get('homeTeam', {}).get('teamTricode', 'UNK')
                    if away_abb != 'UNK' and home_abb != 'UNK':
                        partite_oggi.append((away_abb, home_abb))
                
                st.session_state.partite_oggi = partite_oggi
                if partite_oggi: st.success(f"Trovate {len(partite_oggi)} partite per oggi!")
                else: st.warning("Nessuna partita trovata per oggi. Usa l'inserimento manuale.")
            except Exception as e:
                st.error(f"Errore API Tabellone: {e}")

    st.markdown("---")
    
    col1, col2 = st.columns(2)
    with col1:
        st.subheader("Seleziona Match")
        opzioni_match = ["Inserimento Manuale"] + [f"{away} vs {home}" for away, home in st.session_state.partite_oggi]
        scelta_match = st.selectbox("Partite in programma:", opzioni_match)
        
        if scelta_match == "Inserimento Manuale":
            SQUADRA_ANALIZZATA_ABB = st.text_input("Sigla tua squadra (es. OKC):", max_chars=3).upper()
            OPP_ABB = st.text_input("Sigla squadra avversaria (es. LAL):", max_chars=3).upper()
        else:
            away_abb, home_abb = scelta_match.split(" vs ")
            scelta_sq = st.radio("Quale squadra vuoi analizzare?", [f"{away_abb} (Trasferta)", f"{home_abb} (Casa)"])
            if "(Trasferta)" in scelta_sq:
                SQUADRA_ANALIZZATA_ABB, OPP_ABB = away_abb, home_abb
            else:
                SQUADRA_ANALIZZATA_ABB, OPP_ABB = home_abb, away_abb
                
    with col2:
        st.subheader("Seleziona Giocatori")
        
        if SQUADRA_ANALIZZATA_ABB and OPP_ABB:
            if st.button("🔍 Trova Starters (ESPN)"):
                with st.spinner(f"Cerco il quintetto di {SQUADRA_ANALIZZATA_ABB}..."):
                    st.session_state.starters_list = get_espn_starters(SQUADRA_ANALIZZATA_ABB)
                    if st.session_state.starters_list: st.success("Quintetto base trovato!")
                    else: st.error("Impossibile recuperare gli starters.")
            
            giocatori_scelti = st.multiselect("Seleziona dallo Starting 5:", st.session_state.starters_list)
            extra_giocatori = st.text_input("Oppure aggiungi nomi manualmente (separati da virgola):")
            
            if not st.session_state.team_name_dict:
                st.session_state.team_name_dict = {t['abbreviation']: t['full_name'] for t in teams.get_teams()}
            SQUADRA_ANALIZZATA = st.session_state.team_name_dict.get(SQUADRA_ANALIZZATA_ABB, SQUADRA_ANALIZZATA_ABB)
            OPP_FULL = st.session_state.team_name_dict.get(OPP_ABB, OPP_ABB)

            # --- INTERFACCIA INFORTUNI (ESPN + MANUALI) ---
            st.markdown("---")
            st.subheader("🚑 Gestione Infortuni e Disponibilità")
            
            if st.button("🔄 Scarica Bollettino ESPN"):
                with st.spinner("Scaricamento bollettino medico in corso..."):
                    st.session_state.inj_tua = get_espn_injuries(SQUADRA_ANALIZZATA)
                    st.session_state.inj_avv = get_espn_injuries(OPP_FULL)
                    st.success("Bollettino medico aggiornato!")

            if 'inj_tua' in st.session_state:
                tutti_infortunati = list(set(
                    st.session_state.inj_tua['out'] + st.session_state.inj_tua['dtd'] + 
                    st.session_state.inj_avv['out'] + st.session_state.inj_avv['dtd']
                ))
                if tutti_infortunati:
                    st.session_state.infortunati_disponibili = st.multiselect(
                        "1️⃣ Quali di questi giocatori ESPN consideriamo DISPONIBILI (che giocheranno)?", 
                        tutti_infortunati
                    )
                else:
                    st.info("Nessun infortunio segnalato da ESPN in questa partita.")
                    st.session_state.infortunati_disponibili = []
                    
                st.markdown("**2️⃣ Aggiungi Infortuni Manuali (Non trovati su ESPN)**")
                c_inj1, c_inj2 = st.columns(2)
                with c_inj1:
                    inf_man_tua = st.text_input(f"Assenti in {SQUADRA_ANALIZZATA_ABB}:", help="Es. LeBron James (separati da virgola)")
                with c_inj2:
                    inf_man_avv = st.text_input(f"Assenti in {OPP_ABB}:", help="Es. Stephen Curry (separati da virgola)")
                
                # Salviamo i manuali in memoria
                st.session_state.inf_man_tua_list = [n.strip() for n in inf_man_tua.split(",") if n.strip()]
                st.session_state.inf_man_avv_list = [n.strip() for n in inf_man_avv.split(",") if n.strip()]

            lista_finale_giocatori = list(giocatori_scelti)
            if extra_giocatori:
                lista_finale_giocatori += [n.strip() for n in extra_giocatori.split(",") if n.strip()]

    if st.button("🚀 AVVIA ANALISI SCANNER", type="primary"):
        if not SQUADRA_ANALIZZATA_ABB or not OPP_ABB or not lista_finale_giocatori:
            st.warning("Assicurati di aver inserito squadre e giocatori prima di avviare.")
        else:
            with st.spinner("Analisi e scraping in corso. Attendi..."):
                STAGIONE = "2025-26"
                SQUADRA_ANALIZZATA = st.session_state.team_name_dict.get(SQUADRA_ANALIZZATA_ABB, SQUADRA_ANALIZZATA_ABB)
                OPP_FULL = st.session_state.team_name_dict.get(OPP_ABB, OPP_ABB)

                st.toast("Controllo infortuni e difesa su D&T...")
                
                # --- CALCOLO LISTE INFORTUNATI EFFETTIVI (ESPN - Disponibili + Manuali) ---
                disp = st.session_state.get('infortunati_disponibili', [])
                inj_tua = st.session_state.get('inj_tua', {'out': [], 'dtd': []})
                inj_avv = st.session_state.get('inj_avv', {'out': [], 'dtd': []})
                
                lista_infortunati_squadra = [p for p in inj_tua['out'] + inj_tua['dtd'] if p not in disp] + st.session_state.get('inf_man_tua_list', [])
                lista_infortunati_avversari = [p for p in inj_avv['out'] + inj_avv['dtd'] if p not in disp] + st.session_state.get('inf_man_avv_list', [])

                DEF_DATA = fetch_dunksandthrees_def(lista_infortunati_avversari, OPP_ABB)
                
                all_active_players = players.get_active_players()
                st.success("Dati avversari acquisiti. Inizio proiezioni giocatori!")

                for nome_input in lista_finale_giocatori:
                    p_dict = next((p for p in all_active_players if normalize_name(p['full_name']) == normalize_name(nome_input)), None)
                    if not p_dict:
                        st.error(f"❌ {nome_input}: Giocatore non trovato nell'API NBA.")
                        continue
                        
                    NOME = p_dict['full_name']
                    p_id = p_dict['id']

                    # Se il giocatore era infortunato ma è stato selezionato come DISPONIBILE, questa riga NON lo bloccherà!
                    if any(normalize_name(NOME) == normalize_name(inf) for inf in lista_infortunati_squadra):
                        st.warning(f"🚑 {NOME.upper()}: Segnalato come INFORTUNATO. Salto analisi.")
                        continue
                    
                    try:
                        info = safe_api_call(commonplayerinfo.CommonPlayerInfo, player_id=p_id)
                        if info.empty or 'POSITION' not in info.columns:
                            st.warning(f"⚠️ I server NBA non rispondono per {NOME}. Salto il giocatore per evitare crash.")
                            continue
                        POS = info['POSITION'].values[0]
                        
                        df_logs = safe_api_call(playergamelog.PlayerGameLog, player_id=p_id)
                        if df_logs.empty: continue
                        df_logs['GAME_DATE'] = pd.to_datetime(df_logs['GAME_DATE'])
                        
                        df_f = df_logs.head(10)
                        df_h = df_logs[df_logs['MATCHUP'].str.contains(OPP_ABB)]
                        
                        current_year = datetime.now().year
                        s_str = f"{current_year if datetime.now().month >= 11 else current_year-1}"
                        df_s = df_logs[df_logs['SEASON_ID'].str.contains(s_str)]

                        oggi = datetime.now().date()
                        data_ultima_gara = df_f.iloc[0]['GAME_DATE'].date()
                        giorni_assenza = (oggi - data_ultima_gara).days
                        
                        if giorni_assenza > 24:
                            st.warning(f"⏳ {NOME}: Assente da {giorni_assenza} giorni. Rischio Minutes Restriction.")
                            continue
                            
                        RIENTRO = 10 < giorni_assenza <= 24
                        BACK_TO_BACK = giorni_assenza <= 1

                        # 1. Chiamata alla nuova funzione Ibrida
                        bonus_assoluto, bonus_perc = evaluate_injury_bonus(lista_infortunati_squadra, lista_infortunati_avversari, POS, STAGIONE, DEF_DATA, [])
                        bonus_assoluto = min(bonus_assoluto, 3.0) # Max 3 PRA dai compagni
                        bonus_perc = min(bonus_perc, 0.15)        # Max 15% dalle difese bucate

                        # 2. Calcolo dei modificatori Difensivi (DvP)
                        df_dvp = fetch_dvp_rankings(POS)
                        dvp_row = df_dvp[df_dvp['Team'].str.contains(OPP_FULL, case=False)]
                        ranks = {
                            "PTS": dvp_row['PTS_Rank'].values[0] if not dvp_row.empty else 15,
                            "REB": dvp_row['REB_Rank'].values[0] if not dvp_row.empty else 15,
                            "AST": dvp_row['AST_Rank'].values[0] if not dvp_row.empty else 15
                        }
                        
                        # 3. CREAZIONE DEL DIZIONARIO 'res' (Il motore Base)
                        res = {}
                        for stat in ['PTS', 'REB', 'AST']:
                            res[stat] = calculate_weighted_stat(stat, df_f, df_h, df_s, ranks[stat], RIENTRO)
                            
                        # 4. Calcolo delle medie per la redistribuzione percentuale
                        media_pts = df_s['PTS'].mean() if not df_s['PTS'].empty else 1.0
                        media_reb = df_s['REB'].mean() if not df_s['REB'].empty else 1.0
                        media_ast = df_s['AST'].mean() if not df_s['AST'].empty else 1.0
                        media_pra = media_pts + media_reb + media_ast
                        
                        if media_pra > 0:
                            peso_pts, peso_reb, peso_ast = media_pts/media_pra, media_reb/media_pra, media_ast/media_pra
                        else:
                            peso_pts, peso_reb, peso_ast = 0.60, 0.20, 0.20 

                        # 5. FUSIONE DEI BONUS E TETTO DINAMICO
                        valore_aggiunto_difesa = media_pra * bonus_perc
                        bonus_pra_totale = bonus_assoluto + valore_aggiunto_difesa
                        
                        # L'Hard Cap Intelligente: Il bonus totale non può MAI superare il 10% della media storica del giocatore
                        cap_dinamico = media_pra * 0.10
                        bonus_pra_totale = min(bonus_pra_totale, cap_dinamico)

                        # 6. Applicazione Malus Stanchezza e aggiornamento del dizionario 'res'
                        net_change = bonus_pra_totale - (3.0 if RIENTRO else 0.0) - (1.5 if BACK_TO_BACK else 0.0)
                        
                        if net_change != 0:
                            res['PTS'] = max(0.5, res['PTS'] + (net_change * peso_pts))
                            res['REB'] = max(0.5, res['REB'] + (net_change * peso_reb))
                            res['AST'] = max(0.5, res['AST'] + (net_change * peso_ast))

                        best_play, target_stats = generate_strategic_advice(res, df_s, ranks)

                        stds = {
                            'PTS': df_s['PTS'].std(), 'REB': df_s['REB'].std(), 'AST': df_s['AST'].std(),
                            'PTS+REB': (df_s['PTS'] + df_s['REB']).std(),
                            'PTS+AST': (df_s['PTS'] + df_s['AST']).std(),
                            'AST+REB': (df_s['AST'] + df_s['REB']).std(),
                            'PRA': (df_s['PTS'] + df_s['REB'] + df_s['AST']).std()
                        }
                        
                        dd_prob = calc_double_double_prob(res, stds)
                        td_prob = calc_triple_double_prob(res, stds)

                        st.session_state.proiezioni_giocatori[NOME] = {
                            "stats": {
                                "PTS": res['PTS'], "REB": res['REB'], "AST": res['AST'],
                                "PTS+REB": res['PTS'] + res['REB'],
                                "PTS+AST": res['PTS'] + res['AST'],
                                "AST+REB": res['AST'] + res['REB'],
                                "PRA": sum(res.values())
                            },
                            "stds": stds,
                            "dd_prob": dd_prob,
                            "td_prob": td_prob,
                            "best_play": best_play,
                            "opp": OPP_ABB,
                            "timestamp": datetime.now().isoformat()
                        }

                        salva_nel_database(st.session_state.proiezioni_giocatori)

                        with st.expander(f"📊 Dashboard: {NOME.upper()} vs {OPP_ABB}", expanded=True):
                            st.markdown(f"**Ruolo:** {POS} | **Status:** {'⚠️ Rientro' if RIENTRO else ('😴 B2B' if BACK_TO_BACK else '✅ Riposato')}")
                            c1, c2, c3, c4 = st.columns(4)
                            c1.metric("Punti (PTS)", f"{res['PTS']:.2f}")
                            c2.metric("Rimbalzi (REB)", f"{res['REB']:.2f}")
                            c3.metric("Assist (AST)", f"{res['AST']:.2f}")
                            c4.metric("Bonus Applicato", f"{net_change:+.1f} PRA netti")
                            st.info(f"🎯 **Giocata Consigliata:** {best_play}")

                    except Exception as e:
                        st.error(f"Errore analisi {NOME}: {e}")
            st.balloons()

# ---------------------------------------------------------------------
# PAGINA 2: VALUTATORE QUOTE (EXPECTED VALUE)
# ---------------------------------------------------------------------
elif menu == "2. 📊 Valutatore Quote (EV)":
    st.title("Valutatore Expected Value (EV)")
    
    if not st.session_state.proiezioni_giocatori:
        st.warning("⚠️ Nessun giocatore in memoria! Vai alla Pagina 1 e analizza una partita.")
    else:
        col1, col2 = st.columns(2)
        with col1:
            giocatori_salvati = list(st.session_state.proiezioni_giocatori.keys())
            g_scelto = st.selectbox("Seleziona Giocatore analizzato:", giocatori_salvati)
        with col2:
            stats_disponibili = list(st.session_state.proiezioni_giocatori[g_scelto]["stats"].keys())
            if "Doppia Doppia" not in stats_disponibili: stats_disponibili.append("Doppia Doppia")
            if "Tripla Doppia" not in stats_disponibili: stats_disponibili.append("Tripla Doppia")
            s_scelta = st.selectbox("Su quale statistica vuoi scommettere?", stats_disponibili)
            
        st.markdown(f"🎯 *Il bot aveva consigliato di puntare su: **{st.session_state.proiezioni_giocatori[g_scelto]['best_play']}***")

        # --- NUOVO BLOCCO: MATCHUP E TIMESTAMP ---
        opp = st.session_state.proiezioni_giocatori[g_scelto].get("opp", "Sconosciuta")
        ts_raw = st.session_state.proiezioni_giocatori[g_scelto].get("timestamp", "")
        if ts_raw:
            ts_format = datetime.fromisoformat(ts_raw).strftime("%d/%m/%Y alle %H:%M")
        else:
            ts_format = "N/D"
            
        st.caption(f"⚔️ **Matchup:** vs {opp} | ⏱️ **Ultimo calcolo:** {ts_format}")
        
        st.markdown("### 📊 Statline Proiettata")
        p_stats = st.session_state.proiezioni_giocatori[g_scelto]["stats"]
        
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Punti (PTS)", f"{p_stats['PTS']:.2f}")
        c2.metric("Rimbalzi (REB)", f"{p_stats['REB']:.2f}")
        c3.metric("Assist (AST)", f"{p_stats['AST']:.2f}")
        c4.metric("Totale (PRA)", f"{p_stats['PRA']:.2f}")
        
        stds = st.session_state.proiezioni_giocatori[g_scelto].get('stds', {})
        st.caption(f"**Volatilità (Deviazione Standard):** PTS (±{stds.get('PTS', 0):.1f}) | REB (±{stds.get('REB', 0):.1f}) | AST (±{stds.get('AST', 0):.1f})")
        st.markdown("---")
        
        # --- LOGICA BIFORCATA PER LA DOPPIA/TRIPLA DOPPIA ---
        if s_scelta in ["Doppia Doppia", "Tripla Doppia"]:
            is_td = (s_scelta == "Tripla Doppia")
            prob_evento = st.session_state.proiezioni_giocatori[g_scelto].get('td_prob' if is_td else 'dd_prob', 0)
            
            st.info(f"🏀 **Probabilità statistica di {s_scelta}: {prob_evento*100:.1f}%**")
            
            quota_default = 15.00 if is_td else 2.50
            quota = st.number_input(f"Inserisci la QUOTA per il SI ({s_scelta}):", value=quota_default, step=0.01)
            
            probabilita = prob_evento
            target_vincita = "Doppia Cifra in 3 stats" if is_td else "Doppia Cifra in 2 stats"
            scarto_perc = probabilita - (1 / quota) if quota > 0 else 0
            proiezione_bot = 0 
            sigma = 0
            
        else:
            proiezione_bot = p_stats[s_scelta]
            st.info(f"📈 **La Proiezione del Bot per {s_scelta} è: {proiezione_bot:.2f}**")
            
            col3, col4 = st.columns(2)
            with col3:
                linea = st.number_input(f"Inserisci la LINEA del bookmaker per l'OVER:", value=20.5, step=0.5)
            with col4:
                quota = st.number_input("Inserisci la QUOTA:", value=1.85, step=0.01)
                
            target_vincita = int(linea) + 1
            sigma = stds.get(s_scelta, 0)
            
            if pd.isna(sigma) or sigma == 0:
                probabilita = 1.0 if proiezione_bot >= target_vincita else 0.0
            else:
                z_score = (target_vincita - 0.5 - proiezione_bot) / (sigma * math.sqrt(2.0))
                probabilita = (1.0 - math.erf(z_score)) / 2.0
            
            scarto_perc = (proiezione_bot - target_vincita) / target_vincita

        # --- MOTORE EV E VOTO POTENZIATO ---
        ev = (probabilita * quota) - 1
        
        if s_scelta not in ["Doppia Doppia", "Tripla Doppia"] and proiezione_bot > 0:
            cv = sigma / proiezione_bot
        else:
            cv = 0.25 
            
        # 1. Calcolo del voto base (EV + Volatilità)
        mod_volatilita = (0.25 - cv) * 3.0
        voto_grezzo = 6.0 + (ev * 15) + mod_volatilita
        voto_finale = max(1.0, min(10.0, voto_grezzo))
        
        # 2. HARD CAP: Il filtro per le giocate a bassa probabilità (Longshot)
        # Se la probabilità è un lancio di moneta o peggio, il voto viene limitato "a forza"
        if probabilita < 0.50:
            # Sotto il 50%: non può MAI superare il 6.9 (Fascia "Rischiosa" - 1,50€ max)
            voto_finale = min(voto_finale, 6.9)
        elif probabilita < 0.54:
            # Tra 50% e 53.9%: non può MAI superare il 7.9 (Fascia "Borderline" - 3,00€ max)
            voto_finale = min(voto_finale, 7.9)
        
        # Display Risultati EV
        st.subheader("Resoconto Matematico")
        
        r1, r2, r3, r4 = st.columns(4)
        lbl_target = s_scelta[:3] if s_scelta not in ['Doppia Doppia', 'Tripla Doppia'] else ('TD' if s_scelta == 'Tripla Doppia' else 'DD')
        r1.metric("Obiettivo Reale", f"{target_vincita} {lbl_target}", delta=f"{scarto_perc*100:+.1f}% vs Linea", delta_color="normal")
        r2.metric("Valore Atteso (ROI)", f"{ev:+.2f}")
        r3.metric("Impatto Volatilità", f"{mod_volatilita:+.1f} pts")
        r4.metric("Voto Giocata", f"{voto_finale:.1f} / 10")
        
        # Feedback Visivo
        stelle = "⭐" * int(voto_finale / 2)
        if voto_finale >= 8.8:
            st.success(f"🔥 **GIOCATA DI VALORE ASSOLUTO (Da prendere)** {stelle}")
        elif voto_finale >= 8.0:
            st.info(f"✅ **BUONA GIOCATA (EV Positivo)** {stelle}")
        elif voto_finale >= 7.2:
            st.warning(f"⚠️ **GIOCATA MARGINALE (Vantaggio minimo)** {stelle}")
        else:

            st.error(f"❌ **DA EVITARE (Il banco ha un vantaggio matematico)**")





