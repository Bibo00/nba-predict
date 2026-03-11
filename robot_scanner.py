import pandas as pd
from datetime import datetime
import time
import requests
import math
import io
import unicodedata

# --- LIBRERIE NBA API ---
from nba_api.stats.static import players, teams
from nba_api.stats.endpoints import playergamelog, commonplayerinfo, playerdashboardbygeneralsplits, leaguedashplayerstats, scoreboardv3

# --- LIBRERIE SELENIUM E SCRAPING ---
import os
from webdriver_manager.chrome import ChromeDriverManager
from bs4 import BeautifulSoup
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

DEF_THRESHOLDS = {"PG": 110.77, "SG": 110.86, "SF": 110.01, "PF": 108.87, "C": 106.49}

# --- CARTA D'IDENTITÀ PER INGANNARE LA NBA ---
custom_headers = {
    'Host': 'stats.nba.com',
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36',
    'Accept': 'application/json, text/plain, */*',
    'Accept-Language': 'en-US,en;q=0.5',
    'Referer': 'https://www.nba.com/',
    'Origin': 'https://www.nba.com/',
    'Connection': 'keep-alive',
}

def safe_api_call(endpoint_class, **kwargs):
    tentativi = 5
    attesa = 2.0  
    
    for i in range(tentativi):
        try:
            # Aggiungiamo gli headers e un timeout massimo alla chiamata
            response = endpoint_class(**kwargs, headers=custom_headers, timeout=30)
            return response.get_data_frames()[0]
        except Exception as e:
            if i < tentativi - 1:
                print(f"⚠️ Server NBA non risponde. Ripetiamo tra {attesa} secondi... (Tentativo {i+1}/{tentativi})")
                time.sleep(attesa)
                attesa *= 2  
            else:
                print(f"❌ Fallimento definitivo dopo {tentativi} tentativi per l'endpoint {endpoint_class.__name__}.")
    return pd.DataFrame()

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

# --- LISTA DEI TUOI GIOCATORI PREFERITI ---
GIOCATORI_VIP = [
    "Jaylen Brown",
    "Kawhi Leonard",
    "Darius Garland",
    "Michael Porter Jr",
    "Jalen Brunson",
    "Karl-Anthony Towns",
    "Josh Hart",
    "Tyrese Maxey",
    "Scottie Barnes",
    "Brandon Ingram",
    "Luka Doncic",
    "Deandre Ayton",
    "Lebron James",
    "Austin Reaves",
    "Devin Booker",
    "Josh Giddey",
    "Donovan Mitchell",
    "James Harden",
    "Evan Mobley",
    "Cade Cunningham",
    "Jalen Duren",
    "Giannis Antetokounmpo",
    "Jalen Johnson",
    "Dyson Daniels", 
    "Onyeka Okongwu",
    "Lamelo Ball",
    "Brandon Miller",
    "Moussa Diabate",
    "Bam Adebayo",
    "Paolo Banchero",
    "Desmond Bane",
    "Jamal Murray",
    "Nikola Jokic",
    "Aaron Gordon",
    "Anthony Edwards",
    "Julius Randle",
    "Rudy Gobert",
    "Naz Reid",
    "Shai Gilgeous-Alexander",
    "Chet Holmgren",
    "Keyonte George",
    "Kevin Durant",
    "Alperen Sengun",
    "Amen Thompson",
    "Trey Murphy III",
    "Dejounte Murray",
    "Zion Williamson",
    "Victor Wembanyama",
    "De'Aaron Fox"
    # Aggiungi qui tutti i giocatori che vuoi monitorare in automatico
]

print("🤖 Avvio Robot Scanner...")

# 1. Trova le partite di stanotte
data_oggi = datetime.now().strftime('%Y-%m-%d')
try:
    # Mascheriamo anche la richiesta del tabellone!
    board_data = scoreboardv3.ScoreboardV3(game_date=data_oggi, headers=custom_headers, timeout=30).get_dict()
    games = board_data.get('scoreboard', {}).get('games', [])
except Exception as e:
    print(f"Errore caricamento partite: {e}")
    games = []

partite_oggi = []
for game in games:
    away_abb = game.get('awayTeam', {}).get('teamTricode', 'UNK')
    home_abb = game.get('homeTeam', {}).get('teamTricode', 'UNK')
    if away_abb != 'UNK' and home_abb != 'UNK':
        partite_oggi.append((away_abb, home_abb))

print(f"🏀 Trovate {len(partite_oggi)} partite oggi: {partite_oggi}")

# --- IL BLOCCO SALVAVITA ---
if len(partite_oggi) == 0:
    print("🛑 Nessuna partita trovata (o i server NBA ci stanno bloccando). Chiudo il robot per non fare danni!")
    import sys
    sys.exit() # Spegne il server istantaneamente
# ---------------------------

team_name_dict = {t['abbreviation']: t['full_name'] for t in teams.get_teams()}
all_active_players = players.get_active_players()

# Carica il DB esistente per non cancellare la roba vecchia
db_cloud = carica_e_pulisci_database()

for nome_vip in GIOCATORI_VIP:
    # Trova il giocatore
    p_dict = next((p for p in all_active_players if normalize_name(p['full_name']) == normalize_name(nome_vip)), None)
    if not p_dict: continue
    
    # Scopri in che squadra gioca e se gioca stanotte
    try:
        info = safe_api_call(commonplayerinfo.CommonPlayerInfo, player_id=p_dict['id'])
        if info.empty: continue
        team_abb = info['TEAM_ABBREVIATION'].values[0]
        POS = info['POSITION'].values[0]
    except: continue

    matchup = None
    OPP_ABB = None
    for away, home in partite_oggi:
        if team_abb == away:
            OPP_ABB = home
            matchup = (team_abb, home)
            break
        elif team_abb == home:
            OPP_ABB = away
            matchup = (team_abb, away)
            break
            
    if not OPP_ABB:
        print(f"💤 {nome_vip} ({team_abb}) non gioca stanotte. Salto.")
        continue
        
    print(f"🔥 Analizzo {nome_vip} contro {OPP_ABB}...")
    
    SQUADRA_FULL = team_name_dict.get(team_abb, team_abb)
    OPP_FULL = team_name_dict.get(OPP_ABB, OPP_ABB)
    
    # Recupera Infortuni ESPN
    inj_tua = get_espn_injuries(SQUADRA_FULL)
    inj_avv = get_espn_injuries(OPP_FULL)
    
    # LA REGOLA D'ORO DEL ROBOT: Considera solo gli 'OUT', ignora i 'DTD' (sani)
    lista_infortunati_squadra = inj_tua['out']
    lista_infortunati_avversari = inj_avv['out']
    
    # Se il nostro VIP è OUT, saltiamo
    if any(normalize_name(nome_vip) == normalize_name(inf) for inf in lista_infortunati_squadra):
        print(f"🚑 {nome_vip} è OUT. Salto.")
        continue
        
    # --- CALCOLI (Identici ad app.py) ---
    DEF_DATA = fetch_dunksandthrees_def(lista_infortunati_avversari, OPP_ABB)
    
    df_logs = safe_api_call(playergamelog.PlayerGameLog, player_id=p_dict['id'])
    if df_logs.empty: continue
    df_logs['GAME_DATE'] = pd.to_datetime(df_logs['GAME_DATE'])
    
    df_f = df_logs.head(10)
    df_h = df_logs[df_logs['MATCHUP'].str.contains(OPP_ABB)]
    current_year = datetime.now().year
    s_str = f"{current_year if datetime.now().month >= 11 else current_year-1}"
    df_s = df_logs[df_logs['SEASON_ID'].str.contains(s_str)]
    
    data_ultima_gara = df_f.iloc[0]['GAME_DATE'].date()
    giorni_assenza = (datetime.now().date() - data_ultima_gara).days
    
    if giorni_assenza > 24: continue
    RIENTRO = 10 < giorni_assenza <= 24
    BACK_TO_BACK = giorni_assenza <= 1

    bonus_assoluto, bonus_perc = evaluate_injury_bonus(lista_infortunati_squadra, lista_infortunati_avversari, POS, "2025-26", DEF_DATA, [])
    
    df_dvp = fetch_dvp_rankings(POS)
    dvp_row = df_dvp[df_dvp['Team'].str.contains(OPP_FULL, case=False)]
    ranks = {
        "PTS": dvp_row['PTS_Rank'].values[0] if not dvp_row.empty else 15,
        "REB": dvp_row['REB_Rank'].values[0] if not dvp_row.empty else 15,
        "AST": dvp_row['AST_Rank'].values[0] if not dvp_row.empty else 15
    }
    
    res = {}
    for stat in ['PTS', 'REB', 'AST']:
        res[stat] = calculate_weighted_stat(stat, df_f, df_h, df_s, ranks[stat], RIENTRO)
        
    media_pts = df_s['PTS'].mean() if not df_s['PTS'].empty else 1.0
    media_reb = df_s['REB'].mean() if not df_s['REB'].empty else 1.0
    media_ast = df_s['AST'].mean() if not df_s['AST'].empty else 1.0
    media_pra = media_pts + media_reb + media_ast
    
    peso_pts, peso_reb, peso_ast = (media_pts/media_pra, media_reb/media_pra, media_ast/media_pra) if media_pra > 0 else (0.6, 0.2, 0.2)
    
    valore_aggiunto_difesa = media_pra * bonus_perc
    bonus_pra_totale = min(bonus_assoluto + valore_aggiunto_difesa, media_pra * 0.20)
    net_change = bonus_pra_totale - (3.0 if RIENTRO else 0.0) - (1.5 if BACK_TO_BACK else 0.0)
    
    if net_change != 0:
        res['PTS'] = max(0.5, res['PTS'] + (net_change * peso_pts))
        res['REB'] = max(0.5, res['REB'] + (net_change * peso_reb))
        res['AST'] = max(0.5, res['AST'] + (net_change * peso_ast))

    best_play, target_stats = generate_strategic_advice(res, df_s, ranks)
    
    stds = {
        'PTS': df_s['PTS'].std(), 'REB': df_s['REB'].std(), 'AST': df_s['AST'].std(),
        'PTS+REB': (df_s['PTS'] + df_s['REB']).std(), 'PTS+AST': (df_s['PTS'] + df_s['AST']).std(),
        'AST+REB': (df_s['AST'] + df_s['REB']).std(), 'PRA': (df_s['PTS'] + df_s['REB'] + df_s['AST']).std()
    }
    
    db_cloud[nome_vip] = {
        "stats": {
            "PTS": res['PTS'], "REB": res['REB'], "AST": res['AST'],
            "PTS+REB": res['PTS'] + res['REB'], "PTS+AST": res['PTS'] + res['AST'],
            "AST+REB": res['AST'] + res['REB'], "PRA": sum(res.values())
        },
        "stds": stds,
        "dd_prob": calc_double_double_prob(res, stds),
        "td_prob": calc_triple_double_prob(res, stds),
        "best_play": best_play,
        "opp": OPP_ABB,
        "timestamp": datetime.now().isoformat()
    }

    print(f"✅ {nome_vip} salvato. Pausa di sicurezza di 3 secondi...")
    time.sleep(3)

# Salva tutto sul cloud!
salva_nel_database(db_cloud)
print("✅ Automazione completata con successo! Dati inviati a JSONBin.")
