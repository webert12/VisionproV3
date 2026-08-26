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
import signal
from datetime import datetime, timedelta
from flask import Flask, render_template_string, request, jsonify, session, redirect, url_for, abort
from werkzeug.security import generate_password_hash, check_password_hash
from supabase import create_client, Client

# ================= CONFIGURAÇÕES TELEGRAM & AMBIENTE =================
TOKEN_TELEGRAM = os.getenv("TOKEN_TELEGRAM", "8710725826:AAFuGmF30Ns-G1glrBYir9ggVya9VwQgZAU")
CHAT_ID_TELEGRAM = os.getenv("CHAT_ID_TELEGRAM", "-1003474284931")
ADMIN_EMAIL = os.getenv("ADMIN_EMAIL", "admin@vision.com")

# ================= CONFIGURAÇÕES SUPABASE =================
SUPABASE_URL = os.getenv("SUPABASE_URL", "SUA_URL_DO_SUPABASE")
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "SUA_CHAVE_ANON_OU_SERVICE_ROLE_SUPABASE")
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

USUARIOS_ONLINE = {}
DADOS_USUARIOS = {} 

ULTIMO_MSG_ID_TELEGRAM = None
QUEM_INICIOU_O_BOT = None

def enviar_telegram(mensagem, auto_delete=None):
    global ULTIMO_MSG_ID_TELEGRAM
    try:
        url = f"https://api.telegram.org/bot{TOKEN_TELEGRAM}/sendMessage"
        payload = {"chat_id": CHAT_ID_TELEGRAM, "text": mensagem, "parse_mode": "HTML"}
        r = requests.post(url, json=payload, timeout=10).json()
        if r.get("ok"):
            msg_id = r["result"]["message_id"]
            if "SINAL CONFIRMADO" in mensagem or "🔥" in mensagem or "🎯" in mensagem:
                ULTIMO_MSG_ID_TELEGRAM = msg_id
            if auto_delete:
                threading.Thread(target=deletar_mensagem_atrasada, args=(msg_id, auto_delete)).start()
            return msg_id
        return None
    except Exception as e:
        print(f"Erro Telegram: {e}")
        return None

def deletar_mensagem_atrasada(msg_id, delay):
    if delay > 0: time.sleep(delay)
    try:
        url = f"https://api.telegram.org/bot{TOKEN_TELEGRAM}/deleteMessage"
        payload = {"chat_id": CHAT_ID_TELEGRAM, "message_id": msg_id}
        requests.post(url, json=payload, timeout=5)
    except: pass

# ================= SERVIDOR FLASK =================
APP_SECRET = os.getenv("FLASK_SECRET", "chave_secreta_vision_pro_ultra_premium_v3_security")
app = Flask(__name__)
app.secret_key = APP_SECRET
log = logging.getLogger('werkzeug')
log.setLevel(logging.ERROR)

# ================= TEMPLATES HTML =================
HTML_ADM = """
<!DOCTYPE html>
<html>
<head>
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>GESTOR DE CLIENTES</title>
    <style>
        body { background: #0e1621; color: white; font-family: sans-serif; padding: 10px; }
        .card { background: rgba(23, 33, 43, 0.8); padding: 15px; border-radius: 10px; border: 1px solid #2481cc; margin-bottom: 10px; font-size: 13px; backdrop-filter: blur(5px); }
        .user-header { cursor: pointer; display: flex; justify-content: space-between; align-items: center; padding: 5px 0; }
        .user-header:hover { color: #2481cc; }
        .user-details { display: none; margin-top: 15px; border-top: 1px solid #242f3d; padding-top: 15px; }
        .btn-adm { padding: 8px 12px; border-radius: 5px; text-decoration: none; color: white; font-weight: bold; font-size: 11px; display: inline-block; margin: 5px 2px; border:none; cursor:pointer; }
        .green { background: #2e7d32; } .red { background: #c62828; } .blue { background: #2481cc; }
        h2 { color: #2481cc; text-align: center; }
        input { background: #242f3d; color: white; border: 1px solid #2b5278; padding: 5px; border-radius: 4px; margin-bottom: 5px; width: 100%; box-sizing: border-box; }
        .status-badge { padding: 2px 6px; border-radius: 4px; font-size: 10px; font-weight: bold; margin-left: 5px; }
        .online { background: #4caf50; color: white; }
        .offline { background: #555; color: #ccc; }
    </style>
    <script>
        function toggleUser(id) {
            const el = document.getElementById(id);
            if (el.style.display === "block") {
                el.style.display = "none";
            } else {
                document.querySelectorAll('.user-details').forEach(d => d.style.display = 'none');
                el.style.display = "block";
            }
        }
    </script>
</head>
<body>
    <h2>👥 GESTÃO DE USUÁRIOS</h2>
    <p style="text-align:center; color:#8a99a8;">Total Online: {{ online_count }}</p>
    <a href="/" style="color: #8a99a8; text-decoration:none; display:block; margin-bottom: 20px; text-align: center;">⬅ Voltar ao Painel</a>

    {% for email, info in lista.items() %}
    <div class="card">
        <div class="user-header" onclick="toggleUser('details-{{ loop.index }}')">
            <span>
                <b>{{ email }}</b>
                {% if email in online_list %}
                    <span class="status-badge online">ONLINE</span>
                {% else %}
                    <span class="status-badge offline">OFFLINE</span>
                {% endif %}
            </span>
            <span style="color:#2481cc; font-size: 10px;">Exibir Dados ▾</span>
        </div>

        <div id="details-{{ loop.index }}" class="user-details">
            <div style="margin-bottom:10px;">
                <span style="color:#2481cc;">Assertividade: <b>{{ info.winrate if info.winrate else 0 }}%</b></span><br>
                <span style="color:#8a99a8;">Wins: {{ info.wins }} | Reds: {{ info.reds }}</span>
            </div>
            <form action="/adm/editar" method="POST">
                <input type="hidden" name="email_original" value="{{ email }}">
                <b>E-mail:</b> <input type="text" name="novo_email" value="{{ email }}">
                <b>Nova Senha (deixe em branco para manter):</b> <input type="password" name="nova_senha" placeholder="Alterar senha...">
                <b>Expira em:</b> {{ info.criado_em }}<br><br>
                <button type="submit" class="btn-adm blue">SALVAR ALTERAÇÕES</button>
                <a href="/adm/renovar/{{ email }}" class="btn-adm green">RENOVAR +30 DIAS</a>
                {% if email != admin %}
                <a href="/adm/excluir/{{ email }}" class="btn-adm red" onclick="return confirm('Excluir?')">EXCLUIR</a>
                {% endif %}
            </form>
        </div>
    </div>
    {% endfor %}
</body>
</html>
"""

HTML_TERMOS = """
<!DOCTYPE html>
<html>
<head><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>TERMOS DE USO</title><style>
    body { background: #0e1621; color: white; font-family: sans-serif; padding: 20px; line-height: 1.6; }
    .card { background: #17212b; padding: 25px; border-radius: 15px; max-width: 600px; margin: auto; border: 1px solid #2481cc; }
    h2 { color: #2481cc; border-bottom: 1px solid #242f3d; padding-bottom: 10px; }
    p { font-size: 14px; color: #8a99a8; }
    .btn { display: block; text-align: center; background: #2481cc; color: white; padding: 12px; border-radius: 8px; text-decoration: none; font-weight: bold; margin-top: 20px; }
</style></head>
<body>
    <div class="card">
        <h2>⚖️ TERMOS DE USO E RESPONSABILIDADE</h2>
        <p>1. <b>NATUREZA DO SERVIÇO:</b> O Vision Pro V3 é uma ferramenta de análise estatística baseada em algoritmos de inteligência artificial e indicadores técnicos. Não garantimos lucros.</p>
        <p>2. <b>RISCO DE MERCADO:</b> O mercado financeiro (Forex e Cripto) envolve riscos elevados. Você pode perder parte ou todo o seu capital.</p>
        <p>3. <b>RESPONSABILIDADE:</b> O usuário é o único responsável por suas operações. O software apenas emite alertas baseados em padrões históricos.</p>
        <p>4. <b>LIMITAÇÃO:</b> Não somos uma corretora ou casa de análise financeira regulamentada. Use este bot para fins de auxílio educacional e operacional próprio.</p>
        <a href="/login" class="btn">LI E CONCORDO</a>
    </div>
</body>
</html>
"""

HTML_LOGIN = """
<!DOCTYPE html>
<html>
<head>
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>LOGIN - VISION PRO</title>
    <style>
        body { background: #0e1621; color: white; font-family: sans-serif; display: flex; justify-content: center; align-items: center; min-height: 100vh; margin: 0; }
        .login-card { background: #17212b; padding: 30px; border-radius: 15px; width: 90%; max-width: 350px; text-align: center; border: 1px solid #242f3d; min-height: 400px; display: flex; flex-direction: column; justify-content: center; }
        input { width: 100%; box-sizing: border-box; padding: 15px; margin: 10px 0; border-radius: 8px; border: none; background: #242f3d; color: white; font-size: 16px; }
        button { width: 100%; padding: 15px; background: #2481cc; border: none; color: white; border-radius: 8px; cursor: pointer; font-weight: bold; font-size: 16px; margin-top: 10px; }
        .links { margin-top: 25px; font-size: 14px; }
        a { color: #2481cc; text-decoration: none; margin: 0 10px; }
    </style>
</head>
<body>
    <div class="login-card">
        <h2>VISION PRO V3</h2>
        {% if erro %}<div style="color:#ff5252; margin-bottom:10px;">{{erro}}</div>{% endif %}
        <form method="POST" action="/login">
            <input type="email" name="email" placeholder="E-mail" required>
            <input type="password" name="password" placeholder="Senha" required>
            <button type="submit">ENTRAR NO SISTEMA</button>
        </form>
        <div class="links">
            <a href="/register">Cadastrar</a> | <a href="/termos" style="color:#8a99a8">Termos</a>
        </div>
    </div>
</body>
</html>
"""

HTML_REGISTER = """
<!DOCTYPE html>
<html>
<head>
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>CADASTRO - VISION PRO</title>
    <style>
        body { background: #0e1621; color: white; font-family: sans-serif; display: flex; justify-content: center; align-items: center; min-height: 100vh; margin: 0; }
        .login-card { background: #17212b; padding: 30px; border-radius: 15px; width: 90%; max-width: 350px; text-align: center; border: 1px solid #242f3d; }
        input { width: 100%; box-sizing: border-box; padding: 15px; margin: 10px 0; border-radius: 8px; border: none; background: #242f3d; color: white; font-size: 16px; }
        button { width: 100%; padding: 15px; background: #2e7d32; border: none; color: white; border-radius: 8px; cursor: pointer; font-weight: bold; font-size: 16px; margin-top: 10px; }
        a { color: #2481cc; text-decoration: none; font-size: 14px; display: block; margin-top: 20px; }
    </style>
</head>
<body>
    <div class="login-card">
        <h2>CRIAR CONTA</h2>
        <form method="POST" action="/register">
            <input type="email" name="email" placeholder="Novo E-mail" required>
            <input type="password" name="password" placeholder="Nova Senha" required>
            <button type="submit">FINALIZAR CADASTRO</button>
        </form>
        <a href="/login">Já tenho conta</a>
    </div>
</body>
</html>
"""

HTML_INDEX = """
<!DOCTYPE html>
<html lang="pt-br">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>VISION PRO V3</title>
    <script src="https://unpkg.com/lightweight-charts/dist/lightweight-charts.standalone.production.js"></script>
    <style>
        body { font-family: 'Segoe UI', sans-serif; background: #0b0e14; color: #e1e1e1; margin: 0; padding: 15px; display: flex; justify-content: center; min-height: 100vh; }
        .container { width: 100%; max-width: 480px; background: rgba(23, 33, 43, 0.85); backdrop-filter: blur(15px); padding: 20px; border-radius: 20px; border: 1px solid rgba(255,255,255,0.08); }
        .header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 15px; }
        .status-box { background: rgba(36, 47, 61, 0.6); padding: 15px; border-radius: 15px; margin-bottom: 15px; min-height: 80px; border: 1px solid rgba(36, 129, 204, 0.3); text-align: center; display: flex; flex-direction: column; align-items: center; justify-content: center; }
        
        #chart-wrapper { background: #131722; border-radius: 12px; padding: 10px; border: 1px solid #242f3d; margin-bottom: 15px; }
        #chart-header { display: flex; justify-content: space-between; font-size: 12px; margin-bottom: 8px; color: #2481cc; font-weight: bold; }
        #chart-container { width: 100%; height: 230px; position: relative; }

        .menu-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; margin-top: 10px; }
        .sub-menu { display: none; grid-template-columns: 1fr 1fr 1fr; gap: 8px; padding: 12px; background: rgba(28, 41, 56, 0.5); border-radius: 12px; margin-bottom: 12px; }
        button { border: none; border-radius: 10px; cursor: pointer; font-weight: 600; color: white; transition: 0.2s; }
        .btn-menu { padding: 12px; background: rgba(255,255,255,0.05); border: 1px solid rgba(255,255,255,0.1); font-size: 12px; }
        .btn-opt { padding: 8px; font-size: 11px; background: #2b5278; }
        .btn-active { background: #2481cc !important; }
        .placar-mini { display: flex; flex-direction: column; background: rgba(0,0,0,0.3); padding: 12px; border-radius: 12px; margin-bottom: 15px; font-size: 12px; }
        .placar-row { display: flex; justify-content: space-around; width: 100%; }
        .winrate-bar { height: 5px; background: #333; border-radius: 10px; overflow: hidden; margin-top: 5px; }
        .winrate-fill { height: 100%; background: #4caf50; width: 0%; transition: 0.5s; }
        .historico-box { display: none; background: #1c2938; border-radius: 12px; padding: 12px; margin-top: 12px; }
        .historico-scroll { max-height: 150px; overflow-y: auto; }
        .historico-item { font-size: 11px; padding: 6px 0; border-bottom: 1px solid rgba(255,255,255,0.05); display: flex; justify-content: space-between; }
        .tech-scanner { width: 30px; height: 30px; margin: 5px auto; border: 3px solid rgba(0, 255, 204, 0.1); border-top-color: #00ffcc; border-radius: 50%; animation: spin 1s linear infinite; }
        @keyframes spin { to { transform: rotate(360deg); } }
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <div style="color:#2481cc; font-weight:bold;">VISION PRO V3</div>
            <a href="/logout" style="color:#ff5252; text-decoration:none; font-size:12px;">SAIR</a>
        </div>

        <div class="placar-mini">
            <div class="placar-row">
                <span>WINS: <b id="win-count" style="color:#4caf50">0</b></span>
                <span>ASSERT.: <b id="wr-text" style="color:#2481cc">0%</b></span>
                <span>LOSS: <b id="loss-count" style="color:#ff5252">0</b></span>
            </div>
            <div class="winrate-bar"><div id="wr-fill" class="winrate-fill"></div></div>
        </div>

        <div id="chart-wrapper">
            <div id="chart-header">
                <span id="chart-symbol">EURUSD</span>
                <span id="chart-price" style="color:#00ffcc;">--.--</span>
            </div>
            <div id="chart-container"></div>
        </div>

        <div class="status-box" id="panel-text">Aguardando Comando...</div>

        <div id="result-area" style="display:none; grid-template-columns: 1fr 1fr 1fr 1fr; gap: 8px; margin-bottom: 15px;">
            <button style="background:#2e7d32; padding:10px;" onclick="fetch('/resultado/win')">WIN</button>
            <button style="background:#f9a825; padding:10px;" onclick="fetch('/resultado/g1')">G1</button>
            <button style="background:#c62828; padding:10px;" onclick="fetch('/resultado/red')">RED</button>
            <button style="background:#546e7a; padding:10px;" onclick="fetch('/resultado/pular')">PULAR</button>
        </div>

        <div class="menu-grid">
            <button class="btn-menu" onclick="toggleSub('menu-inicio')">🏠 INÍCIO</button>
            <button class="btn-menu" onclick="toggleSub('menu-historico')">📋 SINAIS</button>
            <button class="btn-menu" onclick="toggleSub('menu-mercado')">💹 MERCADO</button>
            <button class="btn-menu" onclick="toggleSub('menu-times')">🕒 TIMES</button>
            <button class="btn-menu" onclick="toggleSub('menu-estrategias')" style="grid-column: span 2;">⚙️ ESTRATÉGIAS</button>
            {% if user == admin %}
            <button class="btn-menu" onclick="location.href='/admin_panel'" style="grid-column: span 2; background: rgba(36, 129, 204, 0.2);">⚙️ PAINEL ADM</button>
            {% endif %}
        </div>

        <div id="menu-historico" class="historico-box">
            <div class="historico-scroll" id="lista-sinais"></div>
        </div>
        <div id="menu-inicio" class="sub-menu">
            <button class="btn-opt" style="background:#2e7d32" onclick="sendCommand('start_bot')">START</button>
            <button class="btn-opt" style="background:#f57c00" onclick="sendCommand('pause_bot')">PAUSE</button>
            <button class="btn-opt" style="background:#c62828" onclick="sendCommand('stop_bot')">ENCERRAR</button>
        </div>
        <div id="menu-mercado" class="sub-menu">
            <button class="btn-opt {{ 'btn-active' if modo == 'FOREX' }}" onclick="sendCommand('mkt_FOREX')">FOREX</button>
            <button class="btn-opt {{ 'btn-active' if modo == 'CRIPTO' }}" onclick="sendCommand('mkt_CRIPTO')">CRIPTO</button>
            <button class="btn-opt {{ 'btn-active' if modo == 'TODOS' }}" onclick="sendCommand('mkt_TODOS')">TODOS</button>
        </div>
        <div id="menu-times" class="sub-menu">
            <button class="btn-opt {{ 'btn-active' if tf == 1 }}" onclick="sendCommand('tf_1')">M1</button>
            <button class="btn-opt {{ 'btn-active' if tf == 5 }}" onclick="sendCommand('tf_5')">M5</button>
            <button class="btn-opt {{ 'btn-active' if tf == 15 }}" onclick="sendCommand('tf_15')">M15</button>
        </div>
        <div id="menu-estrategias" class="sub-menu" style="grid-template-columns: 1fr 1fr;">
            <button class="btn-opt {{ 'btn-active' if estrat == 'TODAS' }}" onclick="sendCommand('set_est_TODAS')">💎 TODAS (AUTO)</button>
            <button class="btn-opt {{ 'btn-active' if estrat == 'LOGICA_DO_PRECO' }}" onclick="sendCommand('set_est_LOGICA_DO_PRECO')">LÓGICA DO PREÇO</button>
            <button class="btn-opt {{ 'btn-active' if estrat == 'RSI_MACD_MA' }}" onclick="sendCommand('set_est_RSI_MACD_MA')">RSI + MACD + MA</button>
            <button class="btn-opt {{ 'btn-active' if estrat == 'MHI1' }}" onclick="sendCommand('set_est_MHI1')">MHI1</button>
            <button class="btn-opt {{ 'btn-active' if estrat == 'REVERSAO' }}" onclick="sendCommand('set_est_REVERSAO')">REVERSÃO / RETRAÇÃO</button>
        </div>
    </div>

    <script>
        const chartElement = document.getElementById('chart-container');
        const chart = LightweightCharts.createChart(chartElement, {
            layout: { backgroundColor: '#131722', textColor: '#d1d4dc' },
            grid: { vertLines: { color: 'rgba(42, 46, 57, 0.5)' }, horzLines: { color: 'rgba(42, 46, 57, 0.5)' } },
            timeScale: { timeVisible: true, secondsVisible: false }
        });
        const candleSeries = chart.addCandlestickSeries({
            upColor: '#26a69a', downColor: '#ef5350', borderVisible: false, wickUpColor: '#26a69a', wickDownColor: '#ef5350'
        });

        function updateChart() {
            fetch('/api/chart-data').then(r => r.json()).then(res => {
                if(res.data && res.data.length > 0) {
                    candleSeries.setData(res.data);
                    document.getElementById('chart-symbol').innerText = res.symbol;
                    const lastCandle = res.data[res.data.length - 1];
                    document.getElementById('chart-price').innerText = lastCandle.close;
                }
            });
        }
        updateChart();
        setInterval(updateChart, 10000);

        function toggleSub(id) {
            ['menu-inicio', 'menu-mercado', 'menu-times', 'menu-historico', 'menu-estrategias'].forEach(m => {
                const el = document.getElementById(m);
                if(el) el.style.display = (m === id && el.style.display !== 'grid' && el.style.display !== 'block') ? (id === 'menu-historico' ? 'block' : 'grid') : 'none';
            });
        }

        function sendCommand(cmd) {
            fetch('/command/' + cmd).then(r => r.json()).then(data => {
                if(data.redirect) window.location.href = data.redirect;
            });
        }

        setInterval(() => {
            fetch('/status').then(r => r.json()).then(data => {
                const panel = document.getElementById('panel-text');
                if(panel) panel.innerHTML = data.html;
                document.getElementById('win-count').innerText = data.wins;
                document.getElementById('loss-count').innerText = data.reds;
                document.getElementById('wr-text').innerText = data.winrate + "%";
                document.getElementById('wr-fill').style.width = data.winrate + "%";
                document.getElementById('result-area').style.display = data.aguardando ? 'grid' : 'none';
                
                let histHtml = "";
                if(data.historico) {
                    data.historico.forEach(item => {
                        let cor = "#8a99a8";
                        if(item.res.includes("Win")) cor = "#4caf50";
                        if(item.res.includes("Red")) cor = "#ff5252";
                        histHtml += `<div class="historico-item"><span>🕒 ${item.sinal}</span><b style="color:${cor}">${item.res}</b></div>`;
                    });
                }
                document.getElementById('lista-sinais').innerHTML = histHtml || "<div style='text-align:center; font-size:11px; color:#555;'>Nenhum sinal.</div>";
            });
        }, 1500);
    </script>
</body>
</html>
"""

# ================= FUNÇÕES DE BANCO (SUPABASE) =================
def carregar_usuarios():
    try:
        res = supabase.table("usuarios").select("*").execute()
        return {u["email"]: u for u in res.data}
    except Exception as e:
        print(f"Erro ao carregar usuários no Supabase: {e}")
        return {}

def salvar_usuario(email, senha, ip, data=None):
    try:
        data_criacao = data if data else datetime.now().strftime("%Y-%m-%d")
        dados = {
            "email": email,
            "senha": generate_password_hash(senha),
            "criado_em": data_criacao,
            "ip": ip,
            "wins": 0,
            "reds": 0,
            "winrate": 0.0
        }
        supabase.table("usuarios").upsert(dados).execute()
    except Exception as e:
        print(f"Erro ao salvar usuário no Supabase: {e}")

def atualizar_estatisticas_usuario(email, is_win):
    try:
        res = supabase.table("usuarios").select("wins", "reds").eq("email", email).execute()
        if res.data:
            u = res.data[0]
            wins = u["wins"] + (1 if is_win else 0)
            reds = u["reds"] + (0 if is_win else 1)
            total = wins + reds
            winrate = round((wins / total) * 100, 1) if total > 0 else 0.0

            supabase.table("usuarios").update({
                "wins": wins,
                "reds": reds,
                "winrate": winrate
            }).eq("email", email).execute()
    except Exception as e:
        print(f"Erro ao atualizar estatísticas no Supabase: {e}")

def renovar_usuario_db(email):
    try:
        hoje = datetime.now().strftime("%Y-%m-%d")
        supabase.table("usuarios").update({"criado_em": hoje}).eq("email", email).execute()
    except Exception as e:
        print(f"Erro ao renovar usuário no Supabase: {e}")

def excluir_usuario_db(email):
    try:
        if email != ADMIN_EMAIL:
            supabase.table("usuarios").delete().eq("email", email).execute()
    except Exception as e:
        print(f"Erro ao excluir usuário no Supabase: {e}")

def verificar_assinatura(email):
    if email == ADMIN_EMAIL: return True, 999
    try:
        res = supabase.table("usuarios").select("criado_em").eq("email", email).execute()
        if not res.data: return False, 0
        data_criacao = datetime.strptime(res.data[0]["criado_em"], "%Y-%m-%d")
        dias_restantes = 30 - (datetime.now() - data_criacao).days
        return (True, dias_restantes) if dias_restantes > 0 else (False, 0)
    except: return False, 0

def init_user_session(email):
    if email not in DADOS_USUARIOS:
        DADOS_USUARIOS[email] = {
            "sinal_atual": "Aguardando Início..."
        }

def registrar_sinal_bd(email, sinal_str):
    try:
        dados = {"user_email": email, "sinal": sinal_str, "resultado": "Analisando..."}
        supabase.table("historico_sinais").insert(dados).execute()
    except Exception as e:
        print(f"Erro ao registrar sinal no Supabase: {e}")

def buscar_historico_bd(email):
    try:
        res = supabase.table("historico_sinais").select("id, sinal, resultado").eq("user_email", email).order("id", desc=True).limit(10).execute()
        return [{"id": r["id"], "sinal": r["sinal"], "res": r["resultado"]} for r in res.data]
    except Exception as e:
        print(f"Erro ao buscar histórico no Supabase: {e}")
        return []

def atualizar_ultimo_sinal_bd(email, resultado):
    try:
        res = supabase.table("historico_sinais").select("id").eq("user_email", email).order("id", desc=True).limit(1).execute()
        if res.data:
            ultimo_id = res.data[0]["id"]
            supabase.table("historico_sinais").update({"resultado": resultado}).eq("id", ultimo_id).execute()
    except Exception as e:
        print(f"Erro ao atualizar resultado do sinal no Supabase: {e}")

# ================= CONFIGURAÇÕES DO BOT PRO E ESTRATÉGIAS =================
TIMEFRAME_OPERACAO = 5
TIPO_MERCADO = "TODOS"
ESTRATEGIA_ESCOLHIDA = "TODAS"
BACKTEST_AUTO = True
RESULTADOS_BACKTEST = {}

LISTA_ESTRATEGIAS = ["LOGICA_DO_PRECO", "RSI_MACD_MA", "MHI1", "REVERSAO"]

BOT_RODANDO = True
BOT_PAUSADO = True
BOT_INICIADO = False
AG_RESULTADO = False
FUSO = pytz.timezone("America/Sao_Paulo")
IS_ALERT_MODE = False
ULTIMO_SINAL_GLOBAL = "Aguardando Início..."
ATIVO_ATUAL_GLOBAL = "EURUSD=X"

ATIVOS_BASE = {
    "FOREX": ["EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "EURJPY", "USDCAD", "USDCHF", "GBPJPY"],
    "CRIPTO": ["BTCUSD", "ETHUSD", "SOLUSD", "BNBUSD", "XRPUSD", "ADAUSD", "AVAXUSD", "LINKUSD", "DOGEUSD"]
}

MAPA_TICKERS = {}
for par in ATIVOS_BASE["FOREX"]: MAPA_TICKERS[par] = f"{par}=X"
for par in ATIVOS_BASE["CRIPTO"]: MAPA_TICKERS[par] = par.replace("USD", "-USD")

# ================= MOTOR DE ANÁLISE =================
def get_data_v2(ticker, tf, period='5d'):
    try:
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}?interval={tf}m&range={period}"
        res = requests.get(url, headers={'User-Agent': 'Mozilla/5.0'}, timeout=5).json()
        result = res['chart']['result'][0]
        timestamps = result['timestamp']
        ohlc = {
            "time": np.array(timestamps),
            "open": np.array(result['indicators']['quote'][0]['open']),
            "high": np.array(result['indicators']['quote'][0]['high']),
            "low": np.array(result['indicators']['quote'][0]['low']),
            "close": np.array(result['indicators']['quote'][0]['close']),
            "volume": np.array(result['indicators']['quote'][0]['volume'])
        }
        idx = ~np.isnan(ohlc["close"])
        for k in ohlc: ohlc[k] = ohlc[k][idx]
        return ohlc
    except: return None

def analisar_estrategia(data, estrategia, i=-1):
    c, o, h, l = data["close"], data["open"], data["high"], data["low"]
    if len(c) < 50: return None

    if estrategia == "LOGICA_DO_PRECO":
        cor = "G" if c[i] > o[i] else "R"
        tamanho = abs(c[i] - o[i])
        p_sup = h[i] - max(o[i], c[i])
        p_inf = min(o[i], c[i]) - l[i]
        media_corpo = np.mean(abs(c[i-10:i] - o[i-10:i])) if i >= 10 else 0.0001
        if cor == "G" and p_inf == 0 and tamanho > media_corpo: return "CALL"
        if cor == "R" and p_sup == 0 and tamanho > media_corpo: return "PUT"
        if p_inf > tamanho * 2: return "CALL"
        if p_sup > tamanho * 2: return "PUT"

    if estrategia == "RSI_MACD_MA":
        ma = np.mean(c[i-20:i])
        diff = np.diff(c[i-15:i])
        up = diff[diff>0]
        down = diff[diff<0]
        avg_up = np.mean(up) if len(up) > 0 else 0
        avg_down = abs(np.mean(down)) if len(down) > 0 else 0.001
        rs = avg_up / avg_down if avg_down != 0 else 0
        rsi = 100 - (100 / (1 + rs))
        if c[i] > ma and rsi < 30: return "CALL"
        if c[i] < ma and rsi > 70: return "PUT"

    if estrategia == "MHI1":
        cores = [("G" if c[j] > o[j] else "R") for j in range(i-2, i+1)]
        if cores.count("G") > cores.count("R"): return "PUT"
        if cores.count("R") > cores.count("G"): return "CALL"

    if estrategia in ["REVERSAO", "RETRACAO"]:
        std = np.std(c[i-20:i])
        ma = np.mean(c[i-20:i])
        if c[i] < (ma - 2*std): return "CALL"
        if c[i] > (ma + 2*std): return "PUT"

    return None

# ================= ROTAS E API =================
@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        usuarios = carregar_usuarios()
        e, s = request.form['email'], request.form['password']
        if e in usuarios and check_password_hash(usuarios[e]['senha'], s):
            ativo, _ = verificar_assinatura(e)
            if ativo:
                session['user'] = e
                USUARIOS_ONLINE[e] = time.time()
                init_user_session(e)
                return redirect('/')
        return render_template_string(HTML_LOGIN, erro="Acesso Negado ou Conta Inexistente")
    return render_template_string(HTML_LOGIN)

@app.route('/logout')
def logout():
    global QUEM_INICIOU_O_BOT
    user = session.get('user')
    if user in USUARIOS_ONLINE: del USUARIOS_ONLINE[user]
    if user == QUEM_INICIOU_O_BOT: QUEM_INICIOU_O_BOT = None
    session.clear()
    return redirect('/login')

@app.route('/termos')
def termos():
    return render_template_string(HTML_TERMOS)

@app.route('/admin_panel')
def admin_panel():
    if session.get('user') != ADMIN_EMAIL: return abort(403)
    now = time.time()
    for u in list(USUARIOS_ONLINE.keys()):
        if now - USUARIOS_ONLINE[u] > 60: del USUARIOS_ONLINE[u]
    return render_template_string(HTML_ADM, lista=carregar_usuarios(), admin=ADMIN_EMAIL, online_count=len(USUARIOS_ONLINE), online_list=USUARIOS_ONLINE.keys())

@app.route('/adm/renovar/<email>')
def adm_renovar(email):
    if session.get('user') != ADMIN_EMAIL: return abort(403)
    renovar_usuario_db(email)
    return redirect('/admin_panel')

@app.route('/adm/editar', methods=['POST'])
def adm_editar():
    if session.get('user') != ADMIN_EMAIL: return abort(403)
    original, novo_email, nova_senha = request.form['email_original'], request.form['novo_email'], request.form['nova_senha']
    
    try:
        if nova_senha.strip():
            supabase.table("usuarios").update({
                "email": novo_email,
                "senha": generate_password_hash(nova_senha)
            }).eq("email", original).execute()
        else:
            supabase.table("usuarios").update({"email": novo_email}).eq("email", original).execute()
    except Exception as e:
        print(f"Erro ao editar no Supabase: {e}")
        
    return redirect('/admin_panel')

@app.route('/adm/excluir/<email>')
def adm_excluir(email):
    if session.get('user') != ADMIN_EMAIL: return abort(403)
    excluir_usuario_db(email)
    return redirect('/admin_panel')

@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        e, s = request.form['email'], request.form['password']
        if e and s: salvar_usuario(e, s, request.remote_addr); return redirect('/login')
    return render_template_string(HTML_REGISTER)

@app.route('/')
def index():
    if 'user' not in session: return redirect('/login')
    user = session['user']
    USUARIOS_ONLINE[user] = time.time()
    init_user_session(user)
    return render_template_string(HTML_INDEX, modo=TIPO_MERCADO, tf=TIMEFRAME_OPERACAO, estrat=ESTRATEGIA_ESCOLHIDA, user=user, admin=ADMIN_EMAIL)

@app.route('/api/chart-data')
def chart_data():
    data = get_data_v2(ATIVO_ATUAL_GLOBAL, TIMEFRAME_OPERACAO, period='1d')
    chart_list = []
    if data and len(data["close"]) > 0:
        for j in range(len(data["close"])):
            chart_list.append({
                "time": int(data["time"][j]),
                "open": float(data["open"][j]),
                "high": float(data["high"][j]),
                "low": float(data["low"][j]),
                "close": float(data["close"][j])
            })
    return jsonify({"symbol": ATIVO_ATUAL_GLOBAL.replace("=X", ""), "data": chart_list})

@app.route('/status')
def status():
    user = session.get('user')
    if not user: return jsonify({})
    USUARIOS_ONLINE[user] = time.time()
    
    usuarios = carregar_usuarios()
    u_info = usuarios.get(user, {"wins": 0, "reds": 0, "winrate": 0.0})
    historico = buscar_historico_bd(user)
    
    return jsonify({
        "html": ULTIMO_SINAL_GLOBAL, 
        "aguardando": AG_RESULTADO, 
        "wins": u_info.get("wins", 0),
        "reds": u_info.get("reds", 0), 
        "winrate": u_info.get("winrate", 0.0), 
        "historico": historico
    })

@app.route('/command/<cmd>')
def command(cmd):
    global BOT_INICIADO, BOT_PAUSADO, TIMEFRAME_OPERACAO, TIPO_MERCADO, QUEM_INICIOU_O_BOT, ULTIMO_SINAL_GLOBAL, AG_RESULTADO, ESTRATEGIA_ESCOLHIDA
    user = session.get('user')

    if cmd == "start_bot":
        QUEM_INICIOU_O_BOT = user
        BOT_INICIADO, BOT_PAUSADO = True, False
        ULTIMO_SINAL_GLOBAL = "📡 Scanner Ativo...<div class='tech-scanner'></div>"
        enviar_telegram(f"🚀 <b>SISTEMA VISION PRO V3 CONECTADO</b>\nSessão iniciada por {user}")
    elif cmd == "pause_bot":
        BOT_PAUSADO = not BOT_PAUSADO
        ULTIMO_SINAL_GLOBAL = "PAUSADO" if BOT_PAUSADO else "📡 Scanner Ativo...<div class='tech-scanner'></div>"
    elif cmd == "stop_bot":
        BOT_INICIADO, BOT_PAUSADO = False, True
        ULTIMO_SINAL_GLOBAL = "Aguardando Início..."
        return jsonify({"ok": True, "redirect": "/login"})
    elif cmd.startswith("tf_"): TIMEFRAME_OPERACAO = int(cmd.split('_')[1])
    elif cmd.startswith("mkt_"): TIPO_MERCADO = cmd.split('_')[1]
    elif cmd.startswith("set_est_"): ESTRATEGIA_ESCOLHIDA = cmd.replace("set_est_", "")
    return jsonify({"ok": True})

@app.route('/resultado/<res>')
def resultado(res):
    global AG_RESULTADO, ULTIMO_SINAL_GLOBAL
    user = session.get('user')
    if user:
        if res == 'win':
            atualizar_estatisticas_usuario(user, True)
            atualizar_ultimo_sinal_bd(user, "Win")
            enviar_telegram("💎 <b>WIN DIRETO!</b>")
        elif res == 'g1':
            atualizar_estatisticas_usuario(user, True)
            atualizar_ultimo_sinal_bd(user, "WinG1")
            enviar_telegram("🔄 <b>WIN NO GALE 1!</b>")
        elif res == 'red':
            atualizar_estatisticas_usuario(user, False)
            atualizar_ultimo_sinal_bd(user, "Red")
            enviar_telegram("📉 <b>STOP LOSS / RED</b>")
        elif res == 'pular':
            atualizar_ultimo_sinal_bd(user, "Ignorado")

        ULTIMO_SINAL_GLOBAL = "📡 Scanner Ativo...<div class='tech-scanner'></div>"
    AG_RESULTADO = False
    return redirect('/')

# ================= LOOP DO BOT =================
def bot_loop():
    global ULTIMO_SINAL_GLOBAL, AG_RESULTADO, BOT_INICIADO, ATIVO_ATUAL_GLOBAL
    while BOT_RODANDO:
        try:
            if not BOT_INICIADO or BOT_PAUSADO or AG_RESULTADO:
                time.sleep(1); continue

            ativos = ATIVOS_BASE["FOREX"] + ATIVOS_BASE["CRIPTO"] if TIPO_MERCADO == "TODOS" else ATIVOS_BASE[TIPO_MERCADO]
            for ativo in ativos:
                if not BOT_INICIADO or BOT_PAUSADO: break
                ticker = MAPA_TICKERS.get(ativo, ativo)
                ATIVO_ATUAL_GLOBAL = ticker
                
                data = get_data_v2(ticker, TIMEFRAME_OPERACAO)
                if not data: continue

                sinal = None
                if ESTRATEGIA_ESCOLHIDA == "TODAS":
                    for est_nome in LISTA_ESTRATEGIAS:
                        sinal = analisar_estrategia(data, est_nome)
                        if sinal: break
                else:
                    sinal = analisar_estrategia(data, ESTRATEGIA_ESCOLHIDA)

                if sinal:
                    dir_txt = "COMPRA" if sinal == "CALL" else "VENDA"
                    ULTIMO_SINAL_GLOBAL = f"<div style='text-align:center;'>🎯 <b>SINAL CONFIRMADO: {ativo}</b><br>Entrada: {dir_txt}</div>"
                    
                    # Salva o sinal gerado para os usuários no banco
                    for u in USUARIOS_ONLINE.keys():
                        registrar_sinal_bd(u, f"{ativo} (M{TIMEFRAME_OPERACAO})")
                        
                    enviar_telegram(f"🎯 <b>SINAL CONFIRMADO</b>\n\n📈 Ativo: {ativo}\n🕒 Timeframe: M{TIMEFRAME_OPERACAO}\n↕️ Direção: {dir_txt}")
                    AG_RESULTADO = True; break
            time.sleep(2)
        except Exception as e: time.sleep(5)

# Thread daemon rodando em segundo plano
threading.Thread(target=bot_loop, daemon=True).start()

if __name__ == "__main__":
    port = int(os.getenv("PORT", 5001))
    app.run(host='0.0.0.0', port=port)
