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
from flask import Flask, render_template_string, request, jsonify, session, redirect, url_for, abort, send_from_directory
from werkzeug.security import generate_password_hash, check_password_hash

# ================= CONFIGURAÇÕES =================
TOKEN_TELEGRAM = os.getenv("TOKEN_TELEGRAM", "8710725826:AAFuGmF30Ns-G1glrBYir9ggVya9VwQgZAU")
CHAT_ID_TELEGRAM = os.getenv("CHAT_ID_TELEGRAM", "-1003474284931")
ADMIN_EMAIL = os.getenv("ADMIN_EMAIL", "admin@vision.com")

USUARIOS_ONLINE = {}
DADOS_USUARIOS = {}
ULTIMO_MSG_ID_TELEGRAM = None
QUEM_INICIOU_O_BOT = None

def enviar_telegram(mensagem, auto_delete=None):
    global ULTIMO_MSG_ID_TELEGRAM
    if QUEM_INICIOU_O_BOT != ADMIN_EMAIL:
        return None
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

USER_FILE = "usuarios.json"

# ================= DASHBOARD COM GRÁFICO TRADINGVIEW =================
HTML_INDEX = """
<!DOCTYPE html>
<html lang="pt-br">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>VISION PRO V3 - ULTIMATE</title>
    <!-- Lightweight Charts da TradingView via CDN -->
    <script src="https://unpkg.com/lightweight-charts/dist/lightweight-charts.standalone.production.js"></script>
    <style>
        body { font-family: 'Segoe UI', sans-serif; background: #0b0e14; color: #e1e1e1; margin: 0; padding: 15px; display: flex; justify-content: center; min-height: 100vh; }
        .container { width: 100%; max-width: 500px; background: rgba(23, 33, 43, 0.85); backdrop-filter: blur(15px); padding: 20px; border-radius: 20px; border: 1px solid rgba(255,255,255,0.08); }
        .header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 15px; }
        .status-box { background: rgba(36, 47, 61, 0.6); padding: 15px; border-radius: 15px; margin-bottom: 15px; min-height: 80px; border: 1px solid rgba(36, 129, 204, 0.3); text-align: center; display: flex; flex-direction: column; align-items: center; justify-content: center; }
        
        /* Containers do Gráfico Profissional */
        #chart-wrapper { background: #131722; border-radius: 12px; padding: 10px; border: 1px solid #242f3d; margin-bottom: 15px; }
        #chart-header { display: flex; justify-content: space-between; font-size: 12px; margin-bottom: 8px; color: #2481cc; font-weight: bold; }
        #chart-container { width: 100%; height: 260px; position: relative; }

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

        <!-- GRÁFICO PROFISSIONAL -->
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
            <button class="btn-opt" onclick="sendCommand('mkt_FOREX')">FOREX</button>
            <button class="btn-opt" onclick="sendCommand('mkt_CRIPTO')">CRIPTO</button>
            <button class="btn-opt" onclick="sendCommand('mkt_TODOS')">TODOS</button>
        </div>
        <div id="menu-times" class="sub-menu">
            <button class="btn-opt" onclick="sendCommand('tf_1')">M1</button>
            <button class="btn-opt" onclick="sendCommand('tf_5')">M5</button>
            <button class="btn-opt" onclick="sendCommand('tf_15')">M15</button>
        </div>
        <div id="menu-estrategias" class="sub-menu" style="grid-template-columns: 1fr 1fr;">
            <button class="btn-opt" onclick="sendCommand('set_est_TODAS')">💎 TODAS (AUTO)</button>
            <button class="btn-opt" onclick="sendCommand('set_est_LOGICA_DO_PRECO')">LÓGICA DO PREÇO</button>
        </div>
    </div>

    <script>
        // Inicialização do Gráfico TradingView
        const chartElement = document.getElementById('chart-container');
        const chart = LightweightCharts.createChart(chartElement, {
            layout: { backgroundColor: '#131722', textColor: '#d1d4dc' },
            grid: { vertLines: { color: 'rgba(42, 46, 57, 0.5)' }, horzLines: { color: 'rgba(42, 46, 57, 0.5)' } },
            timeScale: { timeVisible: true, secondsVisible: false }
        });
        const candleSeries = chart.addCandlestickSeries({
            upColor: '#26a69a', downColor: '#ef5350', borderVisible: false, wickUpColor: '#26a69a', wickDownColor: '#ef5350'
        });

        function toggleSub(id) {
            ['menu-inicio', 'menu-mercado', 'menu-times', 'menu-historico', 'menu-estrategias'].forEach(m => {
                const el = document.getElementById(m);
                if(el) el.style.display = (m === id && el.style.display !== 'grid' && el.style.display !== 'block') ? (id === 'menu-historico' ? 'block' : 'grid') : 'none';
            });
        }

        function sendCommand(cmd) {
            fetch('/command/' + cmd).then(r => r.json()).then(data => {
                if(data.redirect) window.location.href = data.redirect;
                else location.reload();
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
            });
        }, 1500);
    </script>
</body>
</html>
"""

# [Mantenha aqui as funções auxiliares carregar_usuarios, analisar_estrategia, rotas /login, /status, etc. do seu código original]

if __name__ == "__main__":
    # Inicializa thread do bot
    threading.Thread(target=bot_loop, daemon=True).start()
    
    # Captura a porta atribuída dinamicamente pelo Render
    port = int(os.environ.get("PORT", 5001))
    app.run(host='0.0.0.0', port=port)
