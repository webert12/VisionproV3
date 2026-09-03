import requests
import time
import math
import pytz
import threading
import json
import sys
import random
import os
import logging
import numpy as np
import re
import websocket  # pip install websocket-client
from datetime import datetime, timedelta
from flask import Flask, render_template_string, request, jsonify, session, redirect, abort, Response
from werkzeug.security import generate_password_hash, check_password_hash
import psycopg2
from psycopg2.extras import RealDictCursor

# ================= AJUSTE DE FUSO HORÁRIO (SÃO PAULO / BRASÍLIA) =================
FUSO_SP = pytz.timezone('America/Sao_Paulo')

def agora_brasilia():
    return datetime.now(FUSO_SP)

# ================= CONFIGURAÇÕES DE AMBIENTE =================
TOKEN_TELEGRAM = os.getenv("TOKEN_TELEGRAM", "8710725826:AAFuGmF30Ns-G1glrBYir9ggVya9VwQgZAU")
CHAT_ID_TELEGRAM = os.getenv("CHAT_ID_TELEGRAM", "-1002979466366")
ADMIN_EMAIL = os.getenv("ADMIN_EMAIL", "admin@vision.com").strip().lower()

DB_URL = os.getenv("DB_URL") or os.getenv("DATABASE_URL", "").strip()

def get_db_connection():
    if not DB_URL:
        raise ValueError("A variável de ambiente DB_URL precisa estar configurada.")
    return psycopg2.connect(DB_URL)

USUARIOS_ONLINE = {}
DADOS_USUARIOS = {}
ULTIMO_MSG_ID_TELEGRAM = None
QUEM_INICIOU_O_BOT = ADMIN_EMAIL

def get_client_ip():
    if request.headers.get('X-Forwarded-For'):
        return request.headers.get('X-Forwarded-For').split(',')[0].strip()
    return request.remote_addr

def enviar_telegram(mensagem, auto_delete=None, user_solicitante=None):
    global ULTIMO_MSG_ID_TELEGRAM, QUEM_INICIOU_O_BOT
    if not TOKEN_TELEGRAM or not CHAT_ID_TELEGRAM:
        return None
    
    usuario_ativo = user_solicitante or QUEM_INICIOU_O_BOT or ADMIN_EMAIL
    if usuario_ativo != ADMIN_EMAIL:
        return None

    try:
        url = f"https://api.telegram.org/bot{TOKEN_TELEGRAM}/sendMessage"
        payload = {"chat_id": CHAT_ID_TELEGRAM, "text": mensagem, "parse_mode": "HTML"}
        r = requests.post(url, json=payload, timeout=5).json()
        if r.get("ok"):
            msg_id = r["result"]["message_id"]
            if any(term in mensagem for term in ["SINAL CONFIRMADO", "🔥", "🎯", "Sinal confirmado"]):
                ULTIMO_MSG_ID_TELEGRAM = msg_id
            if auto_delete:
                threading.Thread(target=deletar_mensagem_atrasada, args=(msg_id, auto_delete)).start()
            return msg_id
        return None
    except Exception as e:
        return None

def deletar_mensagem_telegram(msg_id):
    if not TOKEN_TELEGRAM or not CHAT_ID_TELEGRAM or not msg_id: return
    try:
        url = f"https://api.telegram.org/bot{TOKEN_TELEGRAM}/deleteMessage"
        requests.post(url, json={"chat_id": CHAT_ID_TELEGRAM, "message_id": msg_id}, timeout=5)
    except: pass

def deletar_mensagem_atrasada(msg_id, delay):
    if delay > 0: time.sleep(delay)
    deletar_mensagem_telegram(msg_id)

# ================= SERVIDOR FLASK =================
APP_SECRET = os.getenv("FLASK_SECRET", "chave_secreta_vision_pro_ultra_premium_v3_security")
app = Flask(__name__)
app.secret_key = APP_SECRET
log = logging.getLogger('werkzeug')
log.setLevel(logging.ERROR)
NOTIFICACAO_SISTEMA = None

# ================= TEMPLATES HTML =================
HTML_ADM = """<!DOCTYPE html><html><head><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>GESTOR DE CLIENTES</title><style>body { background: #0a0f1d; color: white; font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; padding: 10px; } .card { background: rgba(15, 23, 42, 0.9); padding: 15px; border-radius: 12px; border: 1px solid #00f2fe; margin-bottom: 10px; font-size: 13px; } .user-header { cursor: pointer; display: flex; justify-content: space-between; align-items: center; padding: 5px 0; } .user-details { display: none; margin-top: 15px; border-top: 1px solid #1e293b; padding-top: 15px; } .btn-adm { padding: 10px 14px; border-radius: 6px; text-decoration: none; color: white; font-weight: bold; font-size: 11px; display: inline-block; margin: 5px 2px; border:none; cursor:pointer; } .green { background: #10b981; } .red { background: #ef4444; } .blue { background: #3b82f6; } .orange { background: #f59e0b; } h2 { color: #00f2fe; text-align: center; } input { background: #1e293b; color: white; border: 1px solid #334155; padding: 8px; border-radius: 6px; margin-bottom: 5px; width: 100%; box-sizing: border-box; } .status-badge { padding: 3px 8px; border-radius: 6px; font-size: 10px; font-weight: bold; margin-left: 5px; } .online { background: #10b981; } .offline { background: #475569; }</style><script>function toggleUser(id) { const el = document.getElementById(id); el.style.display = el.style.display === "block" ? "none" : "block"; }</script></head><body><h2>👥 GESTÃO DE USUÁRIOS</h2><p style="text-align:center;">Total Online: {{ online_count }}</p><a href="/" style="color: #00f2fe; display:block; margin-bottom: 20px; text-align: center;">⬅ Voltar</a>{% for email, info in lista.items() %}<div class="card"><div class="user-header" onclick="toggleUser('details-{{ loop.index }}')"><span><b>{{ email }}</b>{% if email in online_list %}<span class="status-badge online">ONLINE</span>{% else %}<span class="status-badge offline">OFFLINE</span>{% endif %}</span></div><div id="details-{{ loop.index }}" class="user-details"><div style="margin-bottom:10px;"><span>Assertividade: <b>{{ info.winrate }}%</b></span><br><span>Wins: {{ info.wins }} | Reds: {{ info.reds }}</span><br><span>IPs: <b>{{ info.ips_Formatados }}</b></span></div><form action="/adm/editar" method="POST"><input type="hidden" name="email_original" value="{{ email }}"><b>E-mail:</b> <input type="text" name="novo_email" value="{{ email }}"><b>Nova Senha:</b> <input type="password" name="nova_senha" placeholder="Alterar senha..."><br><br><button type="submit" class="btn-adm blue">SALVAR</button><a href="/adm/renovar/{{ email }}" class="btn-adm green">RENOVAR</a><a href="/adm/liberar_ip/{{ email }}" class="btn-adm orange">LIBERAR IPS</a>{% if email != admin %}<a href="/adm/excluir/{{ email }}" class="btn-adm red">EXCLUIR</a>{% endif %}</form></div></div>{% endfor %}</body></html>"""
HTML_TERMOS = """<!DOCTYPE html><html><head><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>TERMOS</title><style>body{background:#0a0f1d;color:white;padding:20px;font-family:sans-serif;} .card{background:#0f172a;padding:25px;border-radius:15px;max-width:600px;margin:auto;border:1px solid #00f2fe;} .btn{display:block;text-align:center;background:#0072ff;color:white;padding:14px;border-radius:8px;text-decoration:none;margin-top:20px;}</style></head><body><div class="card"><h2>⚖️ TERMOS DE USO</h2><p>Serviço de análise estatística. O mercado envolve riscos.</p><a href="/login" class="btn">LI E CONCORDO</a></div></body></html>"""
HTML_LOGIN = """<!DOCTYPE html><html><head><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>LOGIN</title><style>body{background:#060913;color:white;display:flex;justify-content:center;align-items:center;min-height:100vh;font-family:sans-serif;} .card{background:rgba(15,23,42,0.95);padding:35px;border-radius:20px;width:90%;max-width:360px;text-align:center;border:1px solid #00f2fe;} input{width:100%;padding:14px;margin:10px 0;border-radius:10px;background:#0f172a;color:white;border:1px solid #1e293b;} button{width:100%;padding:14px;background:#0072ff;color:white;border:none;border-radius:10px;cursor:pointer;} a{color:#00f2fe;text-decoration:none;}</style></head><body><div class="card"><h2>VISION PRO V3</h2>{% if erro %}<div style="color:#ef4444;margin-bottom:15px;">{{erro}}</div>{% endif %}<form method="POST" action="/login"><input type="email" name="email" placeholder="E-mail" required><input type="password" name="password" placeholder="Senha" required><button type="submit">ACESSAR</button></form><br><a href="/register">Criar Conta</a> | <a href="/termos">Termos</a></div></body></html>"""
HTML_REGISTER = """<!DOCTYPE html><html><head><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>CADASTRO</title><style>body{background:#060913;color:white;display:flex;justify-content:center;align-items:center;min-height:100vh;font-family:sans-serif;} .card{background:rgba(15,23,42,0.95);padding:35px;border-radius:20px;width:90%;max-width:360px;text-align:center;border:1px solid #10b981;} input{width:100%;padding:14px;margin:10px 0;border-radius:10px;background:#0f172a;color:white;border:1px solid #1e293b;} button{width:100%;padding:14px;background:#10b981;color:white;border:none;border-radius:10px;cursor:pointer;} a{color:#00f2fe;text-decoration:none;}</style></head><body><div class="card"><h2>CRIAR CONTA</h2>{% if erro %}<div style="color:#ef4444;margin-bottom:15px;">{{erro}}</div>{% endif %}<form method="POST" action="/register"><input type="email" name="email" placeholder="E-mail" required><input type="password" name="password" placeholder="Senha" required><button type="submit">CADASTRAR</button></form><br><a href="/login">Fazer Login</a></div></body></html>"""
HTML_INDEX = """<!DOCTYPE html><html><head><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>VISION PRO V3</title><style>body{background:#060913;color:white;display:flex;justify-content:center;padding:15px;font-family:sans-serif;} .container{width:100%;max-width:520px;background:rgba(15,23,42,0.8);border:1px solid #00f2fe;border-radius:24px;padding:20px;} .header{display:flex;justify-content:space-between;margin-bottom:18px;} .btn-logout{color:#ef4444;text-decoration:none;font-weight:bold;} .placar-grid{display:grid;grid-template-columns:1fr 1fr 1fr;text-align:center;background:#0b1120;padding:15px;border-radius:16px;margin-bottom:15px;} .status-box{background:#0f172a;border:1px solid #00f2fe;padding:18px;border-radius:16px;text-align:center;margin-bottom:15px;min-height:80px;} .btn-action{padding:12px;border:none;border-radius:10px;color:white;cursor:pointer;font-weight:bold;flex:1;} .btn-start{background:#10b981;} .btn-pause{background:#f59e0b;} .btn-stop{background:#ef4444;} .action-flex{display:flex;gap:8px;margin-bottom:15px;} select{width:100%;background:#0f172a;color:white;padding:10px;border-radius:8px;} .broker-iframe-inline{width:100%;height:350px;display:none;border-radius:10px;} .result-grid{display:grid;grid-template-columns:repeat(4,1fr);gap:8px;margin-bottom:15px;display:none;}</style></head><body><div class="container"><div class="header"><b>VISION PRO V3</b><a href="/logout" class="btn-logout">SAIR</a></div><div class="placar-grid"><div>WINS<br><b style="color:#10b981" id="win-count">0</b></div><div>ASSERTIVIDADE<br><b style="color:#3b82f6" id="wr-text">0%</b></div><div>LOSS<br><b style="color:#ef4444" id="loss-count">0</b></div></div><div id="ticker-live-status" style="text-align:center;margin-bottom:10px;font-size:12px;">MERCADO: <b style="color:#00f2fe" id="mkt-badge">TODOS</b> | ATIVO: <b style="color:#38ef7d" id="current-asset">AGUARDANDO...</b></div><div class="status-box" id="panel-text">Aguardando...</div><div class="result-grid" id="result-area"><button class="btn-action btn-start" onclick="fetch('/resultado/win')">WIN</button><button class="btn-action btn-pause" onclick="fetch('/resultado/g1')">G1</button><button class="btn-action btn-stop" onclick="fetch('/resultado/red')">RED</button><button class="btn-action" style="background:#334155;" onclick="fetch('/resultado/pular')">PULAR</button></div><div class="action-flex"><button class="btn-action btn-start" onclick="sendCommand('start_bot')">START</button><button class="btn-action btn-pause" onclick="sendCommand('pause_bot')">PAUSE</button><button class="btn-action btn-stop" onclick="sendCommand('stop_bot')">STOP</button></div><div style="margin-bottom:10px;"><select onchange="sendCommand('mkt_'+this.value)"><option value="TODOS">Analisar Tudo</option><option value="FOREX_ABERTO">Forex Aberto</option><option value="FOREX_OTC">Forex OTC</option></select></div><div style="margin-bottom:10px;"><select onchange="sendCommand('tf_'+this.value)"><option value="1">M1</option><option value="5">M5</option></select></div><div style="margin-bottom:15px;"><select onchange="sendCommand('set_est_'+this.value)"><option value="TODAS">TODAS Estratégias</option><option value="LOGICA_DO_PRECO">Lógica do Preço</option><option value="RSI_MACD_MA">RSI + MACD + MA</option><option value="MHI1">MHI 1 (Minoria)</option><option value="REVERSAO">Reversão de Tendência</option></select></div>{% if user == admin %}<button onclick="location.href='/admin_panel'" style="width:100%;padding:10px;background:#00f2fe;color:#000;font-weight:bold;border-radius:10px;">PAINEL ADM</button>{% endif %}</div><script>function sendCommand(cmd){fetch('/command/'+cmd);} setInterval(()=>{fetch('/status').then(r=>r.json()).then(d=>{if(d.html)document.getElementById('panel-text').innerHTML=d.html; document.getElementById('win-count').innerText=d.wins; document.getElementById('loss-count').innerText=d.reds; document.getElementById('wr-text').innerText=d.winrate+'%'; document.getElementById('current-asset').innerText=d.rodando?d.ativo_atual:"PAUSADO"; document.getElementById('mkt-badge').innerText=d.mercado; document.getElementById('result-area').style.display=d.aguardando?'grid':'none';});}, 1000);</script></body></html>"""

# ================= BANCO DE DADOS =================
def init_db():
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS usuarios (email VARCHAR(255) PRIMARY KEY, senha VARCHAR(255) NOT NULL, criado_em VARCHAR(50) NOT NULL, wins INT DEFAULT 0, reds INT DEFAULT 0, winrate FLOAT DEFAULT 0.0, ips_autorizados VARCHAR(255) DEFAULT '[]');
            CREATE TABLE IF NOT EXISTS historico_sinais (id SERIAL PRIMARY KEY, user_email VARCHAR(255) NOT NULL, sinal VARCHAR(255) NOT NULL, resultado VARCHAR(50) NOT NULL);
        """)
        conn.commit()
        cur.close()
        conn.close()
    except: pass
try: init_db()
except: pass

def parse_ips(ips_raw):
    try: return json.loads(ips_raw) if ips_raw else []
    except: return []

def carregar_usuarios():
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT * FROM usuarios;")
        raw = cur.fetchall()
        cur.close()
        conn.close()
        dict_u = {}
        for u in raw:
            e = u.get("email", "").strip().lower()
            if e:
                d = dict(u)
                d["ips_list"] = parse_ips(d.get("ips_autorizados", "[]"))
                d["ips_Formatados"] = ", ".join(d["ips_list"]) if d["ips_list"] else "Livre"
                dict_u[e] = d
        return dict_u
    except: return {}

def salvar_usuario(email, senha, data=None, ip_inicial=None):
    e = email.strip().lower()
    d = data or agora_brasilia().strftime("%Y-%m-%d")
    s = senha if senha.startswith("scrypt:") else generate_password_hash(senha)
    ips = json.dumps([ip_inicial]) if ip_inicial else "[]"
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("INSERT INTO usuarios (email, senha, criado_em, wins, reds, winrate, ips_autorizados) VALUES (%s, %s, %s, 0, 0, 0.0, %s) ON CONFLICT (email) DO UPDATE SET senha = EXCLUDED.senha;", (e, s, d, ips))
    conn.commit()
    cur.close()
    conn.close()

def atualizar_estatisticas_usuario(email, is_win):
    try:
        e = email.strip().lower()
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT wins, reds FROM usuarios WHERE email = %s;", (e,))
        res = cur.fetchone()
        if res:
            wins = res.get("wins", 0) + (1 if is_win else 0)
            reds = res.get("reds", 0) + (0 if is_win else 1)
            total = wins + reds
            wr = round((wins/total)*100,1) if total>0 else 0.0
            cur.execute("UPDATE usuarios SET wins=%s, reds=%s, winrate=%s WHERE email=%s;", (wins, reds, wr, e))
            conn.commit()
        cur.close()
        conn.close()
    except: pass

def verificar_assinatura(email):
    e = email.strip().lower()
    if e == ADMIN_EMAIL: return True, 999
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT criado_em FROM usuarios WHERE email = %s;", (e,))
        res = cur.fetchone()
        cur.close()
        conn.close()
        if not res: return False, 0
        criado = datetime.strptime(str(res["criado_em"]).split("T")[0], "%Y-%m-%d")
        dias = 30 - (agora_brasilia().replace(tzinfo=None) - criado).days
        return (True, dias) if dias>0 else (False,0)
    except: return True, 30

def registrar_sinal_bd(email, sinal_str):
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("INSERT INTO historico_sinais (user_email, sinal, resultado) VALUES (%s, %s, %s);", (email.strip().lower(), sinal_str, "Analisando..."))
        conn.commit()
        cur.close()
        conn.close()
    except: pass

def atualizar_ultimo_sinal_bd(email, resultado):
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT id FROM historico_sinais WHERE user_email = %s ORDER BY id DESC LIMIT 1;", (email.strip().lower(),))
        res = cur.fetchone()
        if res:
            cur.execute("UPDATE historico_sinais SET resultado = %s WHERE id = %s;", (resultado, res["id"]))
            conn.commit()
        cur.close()
        conn.close()
    except: pass

# ================= CONFIGS BOT & ATIVOS =================
TIMEFRAME_OPERACAO = 5
TIPO_MERCADO = "TODOS"
ESTRATEGIA_ESCOLHIDA = "TODAS"
LISTA_ESTRATEGIAS = ["LOGICA_DO_PRECO", "RSI_MACD_MA", "MHI1", "REVERSAO"]

BOT_RODANDO = True
BOT_PAUSADO = True
BOT_INICIADO = False
AGUARDANDO_CONFIRMACAO_RESULTADO = False
ULTIMO_SINAL_GLOBAL = "Aguardando Comando..."
SINAL_DISPLAY_PERMANENTE = None
ATIVO_ATUAL_GLOBAL = "AGUARDANDO..."
INICIO_VARREDURA_TIME = 0
SINAIS_ENVIADOS = {} 

ATIVOS_BASE = {
    "FOREX_ABERTO": ["EURUSD", "GBPUSD", "USDJPY", "AUDUSD"],
    "FOREX_OTC": ["EURUSD-OTC", "GBPUSD-OTC", "USDJPY-OTC", "AUDUSD-OTC"]
}
MAPA_TICKERS = {}
for par in ATIVOS_BASE["FOREX_ABERTO"]: MAPA_TICKERS[par] = par + "=X"
for par in ATIVOS_BASE["FOREX_OTC"]: MAPA_TICKERS[par] = par

# ================= MOTOR WEBSOCKET CORRETORA (SIMULADOR OTC) =================
WSS_CONFIG = {
    "url": "wss://ws.qxbroker.com/socket.io/?EIO=3&transport=websocket",
    "session_cookie": "INSIRA_SEU_COOKIE_DE_SESSAO_AQUI"
}
OTC_CACHE = {}

def parse_tick_to_ohlc(asset, price, timestamp_ms, tf_minutes):
    if asset not in OTC_CACHE:
        OTC_CACHE[asset] = {"time": [], "open": [], "high": [], "low": [], "close": [], "current_candle": None}
    
    tf_ms = tf_minutes * 60 * 1000
    candle_start_time = timestamp_ms - (timestamp_ms % tf_ms)
    
    cache = OTC_CACHE[asset]
    curr = cache["current_candle"]
    
    if curr is None or curr["time"] < candle_start_time:
        if curr is not None:
            cache["time"].append(curr["time"])
            cache["open"].append(curr["open"])
            cache["high"].append(curr["high"])
            cache["low"].append(curr["low"])
            cache["close"].append(curr["close"])
            if len(cache["close"]) > 50:
                for k in ["time", "open", "high", "low", "close"]: cache[k] = cache[k][-50:]
                
        cache["current_candle"] = {"time": candle_start_time, "open": price, "high": price, "low": price, "close": price}
    else:
        curr["high"] = max(curr["high"], price)
        curr["low"] = min(curr["low"], price)
        curr["close"] = price

def wss_on_message(ws, message):
    try:
        if "tick" in message:
            data = json.loads(message[message.find("["):]) 
            payload = data[1]
            asset = str(payload.get("asset", "")).upper().replace("_", "-")
            price = float(payload.get("price", 0))
            ts = int(payload.get("time", 0))
            if asset and price:
                parse_tick_to_ohlc(asset, price, ts, TIMEFRAME_OPERACAO)
    except: pass

def wss_loop_thread():
    while BOT_RODANDO:
        try:
            ws = websocket.WebSocketApp(WSS_CONFIG["url"], 
                                        cookie=f"session={WSS_CONFIG['session_cookie']}",
                                        on_message=wss_on_message)
            ws.run_forever(ping_interval=25, ping_timeout=10)
        except: time.sleep(5)

# ================= REGRAS DE ANÁLISE =================
def get_data_v2(ticker, tf, velas_minimas=30):
    if "-OTC" in ticker:
        if ticker in OTC_CACHE and len(OTC_CACHE[ticker]["close"]) >= velas_minimas:
            return {
                "time": np.array(OTC_CACHE[ticker]["time"]),
                "open": np.array(OTC_CACHE[ticker]["open"]),
                "high": np.array(OTC_CACHE[ticker]["high"]),
                "low": np.array(OTC_CACHE[ticker]["low"]),
                "close": np.array(OTC_CACHE[ticker]["close"])
            }
        return None
    else:
        try:
            url = f"https://query2.finance.yahoo.com/v8/finance/chart/{ticker}?interval={tf}m&range=5d"
            res = requests.get(url, headers={'User-Agent': 'Mozilla/5.0'}, timeout=5.0).json()
            r = res['chart']['result'][0]
            c = r['indicators']['quote'][0]
            if len(c['close']) >= velas_minimas:
                return {"close": np.array(c['close'], dtype=float), "open": np.array(c['open'], dtype=float), "high": np.array(c['high'], dtype=float), "low": np.array(c['low'], dtype=float)}
        except: return None

def calcular_ema(dados, p):
    ema = np.zeros_like(dados); ema[p-1] = np.mean(dados[:p])
    for i in range(p, len(dados)): ema[i] = (dados[i]-ema[i-1])*(2/(p+1))+ema[i-1]
    return ema

def analisar_estrategia(data, estrategia, i=-1):
    c, o, h, l = data["close"], data["open"], data["high"], data["low"]
    if len(c) < 30: return None
    sinal = None
    
    if estrategia == "LOGICA_DO_PRECO":
        cor = "G" if c[i]>o[i] else "R"; tam = abs(c[i]-o[i])
        psup = max(0, h[i]-max(o[i], c[i])); pinf = max(0, min(o[i], c[i])-l[i])
        if cor=="G" and pinf>=(tam*0.4): sinal="CALL 🟢"
        elif cor=="R" and psup>=(tam*0.4): sinal="PUT 🔴"
        
    elif estrategia == "RSI_MACD_MA":
        # RSI 14
        diff = np.diff(c[-15:])
        rs = (np.mean(diff[diff>0]) if len(diff[diff>0])>0 else 1e-7) / (np.mean(abs(diff[diff<0])) if len(diff[diff<0])>0 else 1e-7)
        rsi = 100 - (100 / (1 + rs))
        
        # MACD (12, 26)
        macd = calcular_ema(c, 12) - calcular_ema(c, 26)
        
        # Média Móvel Exponencial (EMA 20 - Filtro de Tendência)
        ma20 = calcular_ema(c, 20)
        
        # Regras com validação da Média Móvel (MA)
        if c[i] > ma20[i] and rsi < 48 and macd[i] > macd[i-1]:
            sinal = "CALL 🟢"
        elif c[i] < ma20[i] and rsi > 52 and macd[i] < macd[i-1]:
            sinal = "PUT 🔴"
        
    elif estrategia == "MHI1":
        if len(c) >= 3:
            velas_cores = ["G" if c[j] > o[j] else "R" for j in [-3, -2, -1]]
            g_count = velas_cores.count("G")
            r_count = velas_cores.count("R")
            if g_count < r_count: sinal = "CALL 🟢"
            elif r_count < g_count: sinal = "PUT 🔴"
            
    elif estrategia == "REVERSAO":
        if len(c) >= 3:
            if c[-2] > o[-2] and c[-1] > o[-1]: sinal = "PUT 🔴"
            elif c[-2] < o[-2] and c[-1] < o[-1]: sinal = "CALL 🟢"
            
    return sinal

# ================= ROTAS WEB =================
@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        e = request.form.get('email', '').strip().lower(); s = request.form.get('password', '').strip()
        if e == ADMIN_EMAIL: salvar_usuario(e, s)
        users = carregar_usuarios()
        if e in users and check_password_hash(users[e]['senha'], s):
            session['user'] = e
            return redirect('/')
        return render_template_string(HTML_LOGIN, erro="Falha no login.")
    return render_template_string(HTML_LOGIN)

@app.route('/')
def index():
    if 'user' not in session: return redirect('/login')
    return render_template_string(HTML_INDEX, user=session['user'], admin=ADMIN_EMAIL)

@app.route('/status')
def status():
    u = session.get('user')
    info = carregar_usuarios().get(u, {"wins":0,"reds":0,"winrate":0.0}) if u else {}
    return jsonify({"html": SINAL_DISPLAY_PERMANENTE if AGUARDANDO_CONFIRMACAO_RESULTADO else ULTIMO_SINAL_GLOBAL, "aguardando": AGUARDANDO_CONFIRMACAO_RESULTADO, "wins": info.get("wins",0), "reds": info.get("reds",0), "winrate": info.get("winrate",0.0), "ativo_atual": ATIVO_ATUAL_GLOBAL, "mercado": TIPO_MERCADO, "rodando": BOT_INICIADO and not BOT_PAUSADO})

@app.route('/command/<cmd>')
def command(cmd):
    global BOT_INICIADO, BOT_PAUSADO, TIMEFRAME_OPERACAO, TIPO_MERCADO, ESTRATEGIA_ESCOLHIDA, AGUARDANDO_CONFIRMACAO_RESULTADO, QUEM_INICIOU_O_BOT
    u = session.get('user', ADMIN_EMAIL)
    
    if cmd == "start_bot": 
        BOT_INICIADO = True
        BOT_PAUSADO = False
        AGUARDANDO_CONFIRMACAO_RESULTADO = False
        QUEM_INICIOU_O_BOT = u
    elif cmd == "pause_bot": 
        BOT_PAUSADO = not BOT_PAUSADO
    elif cmd == "stop_bot": 
        BOT_INICIADO = False
        BOT_PAUSADO = True
    elif cmd.startswith("tf_"): 
        TIMEFRAME_OPERACAO = int(cmd.split('_')[1])
    elif cmd.startswith("mkt_"): 
        TIPO_MERCADO = cmd.split('_', 1)[1]
    elif cmd.startswith("set_est_"): 
        ESTRATEGIA_ESCOLHIDA = cmd.replace("set_est_", "")
    return jsonify({"ok": True})

@app.route('/resultado/<res>')
def resultado(res):
    global AGUARDANDO_CONFIRMACAO_RESULTADO, SINAL_DISPLAY_PERMANENTE
    u = session.get('user', ADMIN_EMAIL)
    
    if res in ['win', 'g1']:
        atualizar_estatisticas_usuario(u, True)
        atualizar_ultimo_sinal_bd(u, "Win")
        msg_telegram = "<b>✅ GREEN CONFIRMADO! 🚀🎯</b>" if res == 'win' else "<b>✅ WIN NO GALE 1! 🚀🎯</b>"
        enviar_telegram(msg_telegram, user_solicitante=u)
    elif res == 'red':
        atualizar_estatisticas_usuario(u, False)
        atualizar_ultimo_sinal_bd(u, "Red")
        enviar_telegram("<b>❌ RED / LOSS 💔</b>", user_solicitante=u)
    elif res == 'pular':
        enviar_telegram("<b>⚠️ SINAL CANCELADO</b>", user_solicitante=u)

    AGUARDANDO_CONFIRMACAO_RESULTADO = False
    SINAL_DISPLAY_PERMANENTE = None
    return redirect('/')

# ================= LOOP PRINCIPAL DO BOT =================
def bot_loop():
    global ULTIMO_SINAL_GLOBAL, ATIVO_ATUAL_GLOBAL, AGUARDANDO_CONFIRMACAO_RESULTADO, SINAL_DISPLAY_PERMANENTE
    while BOT_RODANDO:
        if not BOT_INICIADO or BOT_PAUSADO or AGUARDANDO_CONFIRMACAO_RESULTADO:
            time.sleep(1); continue
        
        ativos = ATIVOS_BASE.get(TIPO_MERCADO, ATIVOS_BASE["FOREX_ABERTO"]) + (ATIVOS_BASE["FOREX_OTC"] if TIPO_MERCADO == "TODOS" else [])
        for ativo in ativos:
            if not BOT_INICIADO or BOT_PAUSADO or AGUARDANDO_CONFIRMACAO_RESULTADO: break
            ATIVO_ATUAL_GLOBAL = ativo
            ULTIMO_SINAL_GLOBAL = f"<div class='system-console'>🔍 VARRENDO: {ativo} (M{TIMEFRAME_OPERACAO})</div>"
            
            data = get_data_v2(MAPA_TICKERS.get(ativo, ativo), TIMEFRAME_OPERACAO)
            if not data: time.sleep(0.3); continue

            sinal = None
            estrategia_usada = ESTRATEGIA_ESCOLHIDA

            if ESTRATEGIA_ESCOLHIDA == "TODAS":
                for est in LISTA_ESTRATEGIAS:
                    res = analisar_estrategia(data, est)
                    if res:
                        sinal = res
                        estrategia_usada = est
                        break
            else:
                sinal = analisar_estrategia(data, ESTRATEGIA_ESCOLHIDA)
                estrategia_usada = ESTRATEGIA_ESCOLHIDA

            if sinal:
                SINAL_DISPLAY_PERMANENTE = f"<div class='status-box'><h3>🎯 SINAL: {ativo} | {sinal}</h3><p style='font-size:12px;'>Estratégia: {estrategia_usada}</p></div>"
                AGUARDANDO_CONFIRMACAO_RESULTADO = True
                
                # Registra no BD
                registrar_sinal_bd(ADMIN_EMAIL, f"{ativo} | {sinal} ({estrategia_usada})")
                
                # Envia no Canal do Telegram
                msg_telegram = (
                    f"🔥 <b>SINAL CONFIRMADO!</b>\n\n"
                    f"🎯 <b>Ativo:</b> {ativo}\n"
                    f"📊 <b>Entrada:</b> {sinal}\n"
                    f"⏰ <b>Timeframe:</b> M{TIMEFRAME_OPERACAO}\n"
                    f"⚡ <b>Estratégia:</b> {estrategia_usada}"
                )
                enviar_telegram(msg_telegram, user_solicitante=ADMIN_EMAIL)
                break
            time.sleep(0.5)

thread_iniciada = False
lock_thread = threading.Lock()
@app.before_request
def start_background_loop():
    global thread_iniciada
    with lock_thread:
        if not thread_iniciada:
            threading.Thread(target=bot_loop, daemon=True).start()
            threading.Thread(target=wss_loop_thread, daemon=True).start()
            thread_iniciada = True

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
