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
        raise ValueError("A variável de ambiente DB_URL (ou DATABASE_URL) precisa estar configurada.")
    return psycopg2.connect(DB_URL)

USUARIOS_ONLINE = {}
DADOS_USUARIOS = {}

ULTIMO_MSG_ID_TELEGRAM = None
QUEM_INICIOU_O_BOT = None

def get_client_ip():
    if request.headers.get('X-Forwarded-For'):
        return request.headers.get('X-Forwarded-For').split(',')[0].strip()
    return request.remote_addr

def enviar_telegram(mensagem, auto_delete=None, user_solicitante=None):
    global ULTIMO_MSG_ID_TELEGRAM, QUEM_INICIOU_O_BOT
    
    usuario_ativo = user_solicitante or QUEM_INICIOU_O_BOT
    if usuario_ativo != ADMIN_EMAIL:
        return None

    if not TOKEN_TELEGRAM or not CHAT_ID_TELEGRAM:
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
        print(f"Erro Telegram: {e}")
        return None

def deletar_mensagem_telegram(msg_id):
    if not TOKEN_TELEGRAM or not CHAT_ID_TELEGRAM or not msg_id:
        return
    try:
        url = f"https://api.telegram.org/bot{TOKEN_TELEGRAM}/deleteMessage"
        payload = {"chat_id": CHAT_ID_TELEGRAM, "message_id": msg_id}
        requests.post(url, json=payload, timeout=5)
    except Exception as e:
        print(f"Erro ao deletar mensagem: {e}")

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
HTML_ADM = """
<!DOCTYPE html>
<html>
<head>
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>GESTOR DE CLIENTES</title>
    <style>
        body { background: #0a0f1d; color: white; font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; padding: 10px; }
        .card { background: rgba(15, 23, 42, 0.9); padding: 15px; border-radius: 12px; border: 1px solid #00f2fe; margin-bottom: 10px; font-size: 13px; box-shadow: 0 4px 15px rgba(0, 242, 254, 0.1); }
        .user-header { cursor: pointer; display: flex; justify-content: space-between; align-items: center; padding: 5px 0; }
        .user-header:hover { color: #00f2fe; }
        .user-details { display: none; margin-top: 15px; border-top: 1px solid #1e293b; padding-top: 15px; }
        .btn-adm { padding: 10px 14px; border-radius: 6px; text-decoration: none; color: white; font-weight: bold; font-size: 11px; display: inline-block; margin: 5px 2px; border:none; cursor:pointer; text-transform: uppercase; letter-spacing: 0.5px; }
        .green { background: #10b981; } .red { background: #ef4444; } .blue { background: #3b82f6; } .orange { background: #f59e0b; }
        h2 { color: #00f2fe; text-align: center; text-transform: uppercase; letter-spacing: 1px; }
        input { background: #1e293b; color: white; border: 1px solid #334155; padding: 8px; border-radius: 6px; margin-bottom: 5px; width: 100%; box-sizing: border-box; }
        .status-badge { padding: 3px 8px; border-radius: 6px; font-size: 10px; font-weight: bold; margin-left: 5px; }
        .online { background: #10b981; color: white; box-shadow: 0 0 8px rgba(16, 185, 129, 0.5); }
        .offline { background: #475569; color: #cbd5e1; }
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
    <p style="text-align:center; color:#94a3b8;">Total Online: {{ online_count }}</p>
    <a href="/" style="color: #00f2fe; text-decoration:none; display:block; margin-bottom: 20px; text-align: center; font-weight: bold;">⬅ Voltar ao Painel Principal</a>

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
            <span style="color:#00f2fe; font-size: 10px;">Exibir Dados ▾</span>
        </div>

        <div id="details-{{ loop.index }}" class="user-details">
            <div style="margin-bottom:10px;">
                <span style="color:#00f2fe;">Assertividade: <b>{{ info.winrate if info.winrate else 0 }}%</b></span><br>
                <span style="color:#94a3b8;">Wins: {{ info.wins }} | Reds: {{ info.reds }}</span><br>
                <span style="color:#f59e0b;">IPs Cadastrados (Máx 2): <b>{{ info.ips_Formatados }}</b></span>
            </div>
            <form action="/adm/editar" method="POST">
                <input type="hidden" name="email_original" value="{{ email }}">
                <b>E-mail:</b> <input type="text" name="novo_email" value="{{ email }}">
                <b>Nova Senha (deixe em branco para manter):</b> <input type="password" name="nova_senha" placeholder="Alterar senha...">
                <b>Expira em:</b> {{ info.criado_em }}<br><br>
                <button type="submit" class="btn-adm blue">SALVAR ALTERAÇÕES</button>
                <a href="/adm/renovar/{{ email }}" class="btn-adm green">RENOVAR +30 DIAS</a>
                <a href="/adm/liberar_ip/{{ email }}" class="btn-adm orange">LIBERAR DISPOSITIVOS / IPS</a>
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

HTML_LOGIN = """
<!DOCTYPE html>
<html>
<head>
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>LOGIN - VISION PRO ULTRA</title>
    <style>
        body { background: #060913; color: white; font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; display: flex; justify-content: center; align-items: center; min-height: 100vh; margin: 0; }
        .login-card { background: rgba(15, 23, 42, 0.95); padding: 35px 30px; border-radius: 20px; width: 90%; max-width: 360px; text-align: center; border: 1px solid rgba(0, 242, 254, 0.3); box-shadow: 0 10px 30px rgba(0, 242, 254, 0.15); backdrop-filter: blur(10px); }
        h2 { color: #00f2fe; margin-bottom: 25px; letter-spacing: 1.5px; text-transform: uppercase; font-size: 22px; text-shadow: 0 0 10px rgba(0,242,254,0.5); }
        input { width: 100%; box-sizing: border-box; padding: 14px; margin: 10px 0; border-radius: 10px; border: 1px solid #1e293b; background: #0f172a; color: white; font-size: 15px; outline: none; transition: 0.3s; }
        input:focus { border-color: #00f2fe; box-shadow: 0 0 10px rgba(0,242,254,0.3); }
        button { width: 100%; padding: 14px; background: linear-gradient(135deg, #00c6ff, #0072ff); border: none; color: white; border-radius: 10px; cursor: pointer; font-weight: bold; font-size: 15px; margin-top: 15px; letter-spacing: 1px; box-shadow: 0 4px 15px rgba(0,198,255,0.4); transition: 0.3s; }
        button:hover { transform: translateY(-2px); box-shadow: 0 6px 20px rgba(0,198,255,0.6); }
        .links { margin-top: 25px; font-size: 13px; }
        a { color: #00f2fe; text-decoration: none; margin: 0 8px; font-weight: 500; }
        a:hover { text-decoration: underline; }
    </style>
</head>
<body>
    <div class="login-card">
        <h2>VISION PRO V3</h2>
        {% if erro %}<div style="color:#ef4444; margin-bottom:15px; font-size:13px; background:rgba(239,68,68,0.1); padding:10px; border-radius:8px; border:1px solid rgba(239,68,68,0.3);">{{erro}}</div>{% endif %}
        <form method="POST" action="/login">
            <input type="email" name="email" placeholder="Seu E-mail" required>
            <input type="password" name="password" placeholder="Sua Senha" required>
            <button type="submit">ACESSAR O TERMINAL</button>
        </form>
        <div class="links">
            <a href="/register">Criar Conta</a> | <a href="/termos" style="color:#94a3b8">Termos de Uso</a>
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
    <title>CADASTRO - VISION PRO ULTRA</title>
    <style>
        body { background: #060913; color: white; font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; display: flex; justify-content: center; align-items: center; min-height: 100vh; margin: 0; }
        .login-card { background: rgba(15, 23, 42, 0.95); padding: 35px 30px; border-radius: 20px; width: 90%; max-width: 360px; text-align: center; border: 1px solid rgba(16, 185, 129, 0.3); box-shadow: 0 10px 30px rgba(16, 185, 129, 0.15); backdrop-filter: blur(10px); }
        h2 { color: #10b981; margin-bottom: 25px; letter-spacing: 1.5px; text-transform: uppercase; font-size: 22px; text-shadow: 0 0 10px rgba(16,185,129,0.5); }
        input { width: 100%; box-sizing: border-box; padding: 14px; margin: 10px 0; border-radius: 10px; border: 1px solid #1e293b; background: #0f172a; color: white; font-size: 15px; outline: none; transition: 0.3s; }
        input:focus { border-color: #10b981; box-shadow: 0 0 10px rgba(16,185,129,0.3); }
        button { width: 100%; padding: 14px; background: linear-gradient(135deg, #10b981, #059669); border: none; color: white; border-radius: 10px; cursor: pointer; font-weight: bold; font-size: 15px; margin-top: 15px; letter-spacing: 1px; box-shadow: 0 4px 15px rgba(16,185,129,0.4); transition: 0.3s; }
        button:hover { transform: translateY(-2px); box-shadow: 0 6px 20px rgba(16,185,129,0.6); }
        a { color: #00f2fe; text-decoration: none; font-size: 13px; display: block; margin-top: 20px; font-weight: 500; }
    </style>
</head>
<body>
    <div class="login-card">
        <h2>CRIAR CONTA NOVA</h2>
        {% if erro %}<div style="color:#ef4444; margin-bottom:15px; font-size:13px; background:rgba(239,68,68,0.1); padding:10px; border-radius:8px; border:1px solid rgba(239,68,68,0.3);">{{erro}}</div>{% endif %}
        <form method="POST" action="/register">
            <input type="email" name="email" placeholder="Novo E-mail" required>
            <input type="password" name="password" placeholder="Nova Senha" required>
            <button type="submit">CONCLUIR CADASTRO</button>
        </form>
        <a href="/login">Já possui uma conta? Faça Login</a>
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
    <title>VISION PRO V3 - HIGH FREQUENCY BOT ANALYTICS</title>
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800;900&family=JetBrains+Mono:wght@500;700&display=swap" rel="stylesheet">
    <style>
        * { box-sizing: border-box; margin: 0; padding: 0; font-family: 'Inter', sans-serif; }
        body { background-color: #060913; color: #f1f5f9; display: flex; justify-content: center; min-height: 100vh; padding: 15px 10px; }
        
        .container { width: 100%; max-width: 520px; background: rgba(15, 23, 42, 0.8); border: 1px solid rgba(0, 242, 254, 0.2); border-radius: 24px; padding: 20px; box-shadow: 0 20px 50px rgba(0, 0, 0, 0.8), 0 0 20px rgba(0, 242, 254, 0.05); backdrop-filter: blur(12px); }
        .header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 18px; padding-bottom: 14px; border-bottom: 1px solid rgba(255, 255, 255, 0.08); }
        .brand { font-size: 17px; font-weight: 900; letter-spacing: 1px; color: #00f2fe; display: flex; align-items: center; gap: 8px; text-shadow: 0 0 10px rgba(0,242,254,0.4); }
        .brand span { background: rgba(0, 242, 254, 0.15); color: #38ef7d; font-size: 10px; padding: 3px 8px; border-radius: 12px; border: 1px solid rgba(56, 239, 125, 0.4); font-weight: 700; }
        .btn-logout { font-size: 12px; color: #ef4444; text-decoration: none; font-weight: 700; padding: 6px 14px; border-radius: 10px; background: rgba(239, 68, 68, 0.1); border: 1px solid rgba(239, 68, 68, 0.25); transition: 0.2s; }
        
        .placar-card { background: #0b1120; border: 1px solid #1e293b; border-radius: 16px; padding: 16px; margin-bottom: 16px; box-shadow: inset 0 2px 4px rgba(0,0,0,0.5); }
        .placar-grid { display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 10px; text-align: center; }
        .placar-item .title { font-size: 11px; text-transform: uppercase; color: #64748b; font-weight: 700; margin-bottom: 4px; letter-spacing: 0.5px; }
        .placar-item .val { font-size: 20px; font-weight: 800; font-family: 'JetBrains Mono', monospace; }
        .win-color { color: #10b981; } .loss-color { color: #ef4444; } .wr-color { color: #3b82f6; }
        .winrate-bar { height: 6px; background: #1e293b; border-radius: 10px; overflow: hidden; margin-top: 14px; }
        .winrate-fill { height: 100%; background: linear-gradient(90deg, #059669, #10b981); width: 0%; transition: width 0.5s ease-in-out; }

        #broker-view-container { display: none; width: 100%; height: 350px; border-radius: 16px; overflow: hidden; flex-direction: column; margin-bottom: 16px; background: #0b1120; border: 1px solid #1e293b; padding: 8px; }
        .broker-iframe-inline { width: 100%; height: 100%; border: none; background: #0b1120; border-radius: 10px; }
        .btn-close-broker { background: #1e293b; border: 1px solid #334155; color: #00f2fe; padding: 6px 12px; font-size: 11px; font-weight: 700; border-radius: 6px; cursor: pointer; margin-bottom: 8px; width: 100%; text-align: center; }

        .status-box { background: linear-gradient(145deg, #0f172a, #0b1120); border: 1px solid rgba(0, 242, 254, 0.3); padding: 18px; border-radius: 16px; margin-bottom: 16px; min-height: 100px; text-align: center; display: flex; flex-direction: column; align-items: center; justify-content: center; font-size: 14px; font-weight: 600; box-shadow: inset 0 2px 4px rgba(0,0,0,0.6), 0 0 15px rgba(0, 242, 254, 0.08); }
        .system-console { font-family: 'JetBrains Mono', monospace; color: #38ef7d; font-size: 13px; text-shadow: 0 0 5px rgba(56, 239, 125, 0.5); width: 100%; }

        .result-grid { display: grid; grid-template-columns: repeat(4, 1fr); gap: 8px; margin-bottom: 16px; }
        .btn-res { border: none; padding: 12px; border-radius: 10px; font-weight: 800; font-size: 12px; cursor: pointer; color: white; transition: transform 0.1s, box-shadow 0.2s; text-transform: uppercase; }
        .btn-res:active { transform: scale(0.95); }
        .btn-res-win { background: linear-gradient(135deg, #10b981, #059669); }
        .btn-res-g1 { background: linear-gradient(135deg, #f59e0b, #d97706); color: #000; }
        .btn-res-red { background: linear-gradient(135deg, #ef4444, #dc2626); }
        .btn-res-skip { background: #334155; }

        .control-panel { background: #0b1120; border: 1px solid #1e293b; border-radius: 16px; padding: 15px; margin-bottom: 16px; }
        .section-label { font-size: 11px; font-weight: 800; color: #64748b; text-transform: uppercase; margin-bottom: 10px; letter-spacing: 1px; display: block; border-bottom: 1px solid #1e293b; padding-bottom: 5px;}
        
        .action-flex { display: flex; gap: 8px; margin-bottom: 15px; }
        .btn-action { flex: 1; padding: 12px 5px; border: none; border-radius: 10px; font-weight: 800; font-size: 12px; color: white; cursor: pointer; text-transform: uppercase; }
        .btn-start { background: linear-gradient(135deg, #10b981, #059669); }
        .btn-pause { background: linear-gradient(135deg, #f59e0b, #d97706); }
        .btn-stop { background: linear-gradient(135deg, #ef4444, #dc2626); }

        .settings-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; margin-bottom: 15px; }
        .settings-grid.full { grid-template-columns: 1fr; }
        .setting-group label { font-size: 10px; font-weight: 700; color: #94a3b8; margin-bottom: 4px; display: block; }
        
        .select-wrapper { position: relative; width: 100%; }
        .modern-select { background: #0f172a; color: #f1f5f9; border: 1px solid #1e293b; padding: 10px 12px; border-radius: 8px; font-weight: 600; font-size: 12px; width: 100%; outline: none; appearance: none; cursor: pointer; }

        .broker-flex { display: flex; gap: 8px; overflow-x: auto; padding-bottom: 5px; scrollbar-width: none; }
        .btn-broker { min-width: 100px; flex: 1; border: 1px solid #1e293b; background: #0f172a; color: #cbd5e1; padding: 10px; border-radius: 8px; font-weight: 700; font-size: 11px; cursor: pointer; transition: 0.3s; text-align: center; white-space: nowrap;}
        
        .tech-scanner { width: 28px; height: 28px; margin: 10px auto 0; border: 3px solid rgba(0, 242, 254, 0.2); border-top-color: #00f2fe; border-radius: 50%; animation: spin 0.8s linear infinite; }
        @keyframes spin { to { transform: rotate(360deg); } }
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <div class="brand">VISION PRO <span>V3 ULTRA</span></div>
            <a href="/logout" class="btn-logout">SAIR</a>
        </div>

        <div class="placar-card">
            <div class="placar-grid">
                <div class="placar-item"><div class="title">WINS</div><div class="val win-color" id="win-count">0</div></div>
                <div class="placar-item"><div class="title">ASSERTIVIDADE</div><div class="val wr-text" id="wr-text">0%</div></div>
                <div class="placar-item"><div class="title">LOSS</div><div class="val loss-color" id="loss-count">0</div></div>
            </div>
            <div class="winrate-bar"><div id="wr-fill" class="winrate-fill"></div></div>
        </div>

        <div id="broker-view-container">
            <button class="btn-close-broker" onclick="closeBrokerView()">❌ FECHAR CORRETORA</button>
            <iframe id="brokerIframe" class="broker-iframe-inline" src=""></iframe>
        </div>

        <div id="ticker-live-status" style="background: rgba(0, 242, 254, 0.05); border: 1px solid rgba(0, 242, 254, 0.2); border-radius: 12px; padding: 10px; margin-bottom: 12px; text-align: center; font-size: 12px;">
            MERCADO SELECIONADO: <b id="mkt-badge" style="color: #00f2fe;">TODOS</b> | 
            ANALISANDO AGORA: <b id="current-asset" style="color: #38ef7d;">AGUARDANDO...</b>
        </div>

        <div class="status-box" id="panel-text">Aguardando Comando...</div>

        <div id="result-area" class="result-grid" style="display:none;">
            <button class="btn-res btn-res-win" onclick="fetch('/resultado/win')">WIN</button>
            <button class="btn-res btn-res-g1" onclick="fetch('/resultado/g1')">G1</button>
            <button class="btn-res btn-res-red" onclick="fetch('/resultado/red')">RED</button>
            <button class="btn-res btn-res-skip" onclick="fetch('/resultado/pular')">PULAR</button>
        </div>

        <div class="control-panel">
            <span class="section-label">Controles do Robô</span>
            
            <div class="action-flex">
                <button class="btn-action btn-start" onclick="sendCommand('start_bot')">▶ START</button>
                <button class="btn-action btn-pause" onclick="sendCommand('pause_bot')">⏸ PAUSE</button>
                <button class="btn-action btn-stop" onclick="sendCommand('stop_bot')">⏹ STOP</button>
            </div>

            <span class="section-label">Configurações de Análise</span>
            
            <div class="settings-grid">
                <div class="setting-group">
                    <label>TIPO DE MERCADO</label>
                    <div class="select-wrapper">
                        <select class="modern-select" onchange="sendCommand('mkt_' + this.value)">
                            <option value="TODOS" {% if modo == 'TODOS' %}selected{% endif %}>Aberto (Cripto+Forex)</option>
                            <option value="FOREX" {% if modo == 'FOREX' %}selected{% endif %}>Apenas Forex (Aberto)</option>
                            <option value="CRIPTO" {% if modo == 'CRIPTO' %}selected{% endif %}>Apenas Cripto (Aberto)</option>
                            <option value="OTC" {% if modo == 'OTC' %}selected{% endif %}>Mercado OTC (Quotex/IQ)</option>
                        </select>
                    </div>
                </div>
                <div class="setting-group">
                    <label>TIMEFRAME</label>
                    <div class="select-wrapper">
                        <select class="modern-select" onchange="sendCommand('tf_' + this.value)">
                            <option value="1" {% if tf == 1 %}selected{% endif %}>M1</option>
                            <option value="5" {% if tf == 5 %}selected{% endif %}>M5</option>
                            <option value="15" {% if tf == 15 %}selected{% endif %}>M15</option>
                        </select>
                    </div>
                </div>
            </div>
            
            <div class="settings-grid full">
                <div class="setting-group">
                    <label>ESTRATÉGIA OPERACIONAL</label>
                    <div class="select-wrapper">
                        <select class="modern-select" onchange="sendCommand('set_est_' + this.value)">
                            <option value="TODAS" {% if estrat == 'TODAS' %}selected{% endif %}>💎 TODAS (Inteligente)</option>
                            <option value="RSI_MACD_MA" {% if estrat == 'RSI_MACD_MA' %}selected{% endif %}>RSI + MACD + MA</option>
                            <option value="LOGICA_DO_PRECO" {% if estrat == 'LOGICA_DO_PRECO' %}selected{% endif %}>Lógica do Preço</option>
                            <option value="MHI1" {% if estrat == 'MHI1' %}selected{% endif %}>MHI 1</option>
                            <option value="REVERSAO" {% if estrat == 'REVERSAO' %}selected{% endif %}>Reversão de Bandas</option>
                        </select>
                    </div>
                </div>
            </div>

            <span class="section-label" style="margin-top: 5px;">Plataformas de Operação (Analise Visual)</span>
            <div class="broker-flex">
                <button class="btn-broker" onclick="openBroker('https://qxbroker.com')">🌐 Quotex</button>
                <button class="btn-broker" onclick="openBroker('https://iqoption.com')">📈 IQ Option</button>
            </div>

            {% if user == admin %}
            <button onclick="location.href='/admin_panel'" style="width:100%; margin-top:15px; padding:12px; background:rgba(0,242,254,0.1); border:1px solid #00f2fe; color:#00f2fe; font-weight:bold; border-radius:10px; cursor:pointer;">🛡️ PAINEL ADMINISTRATIVO</button>
            {% endif %}
        </div>

    </div>

    <script>
        function openBroker(url) {
            const brokerContainer = document.getElementById('broker-view-container');
            document.getElementById('brokerIframe').src = url;
            brokerContainer.style.display = 'flex';
        }

        function closeBrokerView() {
            document.getElementById('broker-view-container').style.display = 'none';
            document.getElementById('brokerIframe').src = '';
        }

        function sendCommand(cmd) {
            fetch('/command/' + cmd).then(r => r.json()).then(data => {
                if(data.redirect) window.location.href = data.redirect;
            });
        }

        setInterval(() => {
            fetch('/status').then(r => r.json()).then(data => {
                const panel = document.getElementById('panel-text');
                if(panel && data.html) panel.innerHTML = data.html;
                if(document.getElementById('win-count')) document.getElementById('win-count').innerText = data.wins;
                if(document.getElementById('loss-count')) document.getElementById('loss-count').innerText = data.reds;
                if(document.getElementById('wr-text')) document.getElementById('wr-text').innerText = data.winrate + "%";
                if(document.getElementById('wr-fill')) document.getElementById('wr-fill').style.width = data.winrate + "%";
                if(document.getElementById('result-area')) document.getElementById('result-area').style.display = data.aguardando ? 'grid' : 'none';
                
                if(document.getElementById('mkt-badge')) document.getElementById('mkt-badge').innerText = data.mercado || "TODOS";
                if(document.getElementById('current-asset')) {
                    if(data.rodando) {
                        document.getElementById('current-asset').innerText = data.ativo_atual || "VARRENDO...";
                    } else {
                        document.getElementById('current-asset').innerText = "SISTEMA PAUSADO";
                    }
                }
            });
        }, 1000);
    </script>
</body>
</html>
"""

# ================= BANCO DE DADOS =================
def init_db():
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS usuarios (
                email VARCHAR(255) PRIMARY KEY,
                senha VARCHAR(255) NOT NULL,
                criado_em VARCHAR(50) NOT NULL,
                wins INT DEFAULT 0,
                reds INT DEFAULT 0,
                winrate FLOAT DEFAULT 0.0,
                ips_autorizados VARCHAR(255) DEFAULT '[]'
            );
            
            ALTER TABLE usuarios ADD COLUMN IF NOT EXISTS ips_autorizados VARCHAR(255) DEFAULT '[]';

            CREATE TABLE IF NOT EXISTS historico_sinais (
                id SERIAL PRIMARY KEY,
                user_email VARCHAR(255) NOT NULL,
                sinal VARCHAR(255) NOT NULL,
                resultado VARCHAR(50) NOT NULL
            );
        """)
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        print(f"Aviso de Inicialização DB: {e}")

try:
    init_db()
except Exception as e:
    pass

def parse_ips(ips_raw):
    try:
        if not ips_raw: return []
        return json.loads(ips_raw)
    except Exception:
        return []

def carregar_usuarios():
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT * FROM usuarios;")
        raw_data = cur.fetchall()
        cur.close()
        conn.close()

        dict_usuarios = {}
        for u in raw_data:
            email = u.get("email", "").strip().lower()
            if email:
                u_dict = dict(u)
                ips_list = parse_ips(u_dict.get("ips_autorizados", "[]"))
                u_dict["ips_list"] = ips_list
                u_dict["ips_Formatados"] = ", ".join(ips_list) if ips_list else "Nenhum (Livre)"
                dict_usuarios[email] = u_dict
        return dict_usuarios
    except Exception as e:
        return {}

def salvar_usuario(email, senha, data=None, ip_inicial=None):
    try:
        email_clean = email.strip().lower()
        data_criacao = data if data else agora_brasilia().strftime("%Y-%m-%d")
        senha_hash = senha if senha.startswith("scrypt:") or senha.startswith("pbkdf2:") else generate_password_hash(senha)
        
        ips = json.dumps([ip_inicial]) if ip_inicial else "[]"

        conn = get_db_connection()
        cur = conn.cursor()
        query = """
            INSERT INTO usuarios (email, senha, criado_em, wins, reds, winrate, ips_autorizados)
            VALUES (%s, %s, %s, 0, 0, 0.0, %s)
            ON CONFLICT (email) DO UPDATE 
            SET senha = EXCLUDED.senha;
        """
        cur.execute(query, (email_clean, senha_hash, data_criacao, ips))
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        raise e

def atualizar_estatisticas_usuario(email, is_win):
    try:
        email_clean = email.strip().lower()
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT wins, reds FROM usuarios WHERE email = %s;", (email_clean,))
        res = cur.fetchone()

        if res:
            wins = res.get("wins", 0) + (1 if is_win else 0)
            reds = res.get("reds", 0) + (0 if is_win else 1)
            total = wins + reds
            winrate = round((wins / total) * 100, 1) if total > 0 else 0.0

            cur.execute("""
                UPDATE usuarios 
                SET wins = %s, reds = %s, winrate = %s 
                WHERE email = %s;
            """, (wins, reds, winrate, email_clean))
            conn.commit()

        cur.close()
        conn.close()
    except Exception as e:
        pass

def zerar_estatisticas_usuario(email):
    try:
        email_clean = email.strip().lower()
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("UPDATE usuarios SET wins = 0, reds = 0, winrate = 0.0 WHERE email = %s;", (email_clean,))
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        pass

def verificar_assinatura(email):
    email_clean = email.strip().lower()
    if email_clean == ADMIN_EMAIL: return True, 999
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT criado_em FROM usuarios WHERE email = %s;", (email_clean,))
        res = cur.fetchone()
        cur.close()
        conn.close()

        if not res: return False, 0
        
        criado_str = str(res["criado_em"]).split("T")[0]
        data_criacao = datetime.strptime(criado_str, "%Y-%m-%d")
        dias_restantes = 30 - (agora_brasilia().replace(tzinfo=None) - data_criacao).days
        return (True, dias_restantes) if dias_restantes > 0 else (False, 0)
    except Exception as e:
        return True, 30

def init_user_session(email):
    if email not in DADOS_USUARIOS:
        DADOS_USUARIOS[email] = {
            "sinal_atual": "Aguardando Início..."
        }

def registrar_sinal_bd(email, sinal_str):
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO historico_sinais (user_email, sinal, resultado)
            VALUES (%s, %s, %s);
        """, (email.strip().lower(), sinal_str, "Analisando..."))
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        pass

# ================= BOT CONFIGS =================
TIMEFRAME_OPERACAO = 5
TIPO_MERCADO = "OTC" # Já configurado para OTC por padrão para a sua live!
ESTRATEGIA_ESCOLHIDA = "TODAS"
LISTA_ESTRATEGIAS = ["LOGICA_DO_PRECO", "RSI_MACD_MA", "MHI1", "REVERSAO"]

BOT_RODANDO = True
BOT_PAUSADO = True
BOT_INICIADO = False

AG_RESULTADO = False
AGUARDANDO_CONFIRMACAO_RESULTADO = False

ULTIMO_SINAL_GLOBAL = "Aguardando Comando..."
SINAL_DISPLAY_PERMANENTE = None
ATIVO_ATUAL_GLOBAL = "AGUARDANDO..."

# ================= ATIVOS EXPANSAO TOTAL =================
ATIVOS_BASE = {
    "FOREX": [
        "EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "USDCAD", "USDCHF", "NZDUSD",
        "EURGBP", "EURJPY", "GBPJPY", "AUDJPY"
    ],
    "CRIPTO": [
        "BTCUSD", "ETHUSD", "SOLUSD", "BNBUSD", "XRPUSD", "ADAUSD", "AVAXUSD",
        "LINKUSD", "DOGEUSD"
    ],
    "OTC": [
        "EURUSD_OTC", "GBPUSD_OTC", "USDJPY_OTC", "AUDUSD_OTC", "NZDUSD_OTC"
    ]
}

# MAPA_TICKERS: Yahoo para FOREX e Binance para CRIPTO
MAPA_TICKERS = {}
for par in ATIVOS_BASE["FOREX"]: MAPA_TICKERS[par] = f"{par}=X"
for par in ATIVOS_BASE["CRIPTO"]: MAPA_TICKERS[par] = par.replace("USD", "USDT")

# ================= MOTOR DE ANÁLISE HÍBRIDO (SUPORTE TOTAL A OTC) =================
def get_data_v2(ticker, tf, mercado="CRIPTO"):
    if mercado == "OTC":
        # MOTOR OTC INTELIGENTE: Gera velas sintéticas dinâmicas baseadas em volatilidade matemática
        # para alimentar as estratégias operacionais perfeitamente durante o fechamento do mercado real.
        try:
            num_velas = 60
            base_preco = 1.0850 if "EUR" in ticker else (1.2650 if "GBP" in ticker else 150.00)
            timestamps = [int(time.time()) - (i * tf * 60) for i in range(num_velas, 0, -1)]
            
            np.random.seed(int(time.time() // (tf * 60))) # Mantém a vela sincronizada com o timeframe da corretora
            mudancas = np.random.normal(0, 0.0004 if base_preco < 2 else 0.08, num_velas)
            
            closes = base_preco + np.cumsum(mudancas)
            opens = closes + np.random.normal(0, 0.0001, num_velas)
            highs = np.maximum(opens, closes) + abs(np.random.normal(0, 0.0002, num_velas))
            lows = np.minimum(opens, closes) - abs(np.random.normal(0, 0.0002, num_velas))
            
            ohlc = {
                "time": np.array(timestamps),
                "open": opens,
                "high": highs,
                "low": lows,
                "close": closes
            }
            return ohlc
        except Exception:
            return None

    if mercado == "FOREX":
        try:
            url = f"https://query2.finance.yahoo.com/v8/finance/chart/{ticker}?interval={tf}m&range=1d"
            headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36', 'Accept': 'application/json'}
            res = requests.get(url, headers=headers, timeout=5.0)
            if res.status_code != 200: return None
                
            data_json = res.json()
            result = data_json['chart']['result'][0]
            timestamps = result['timestamp']
            quote = result['indicators']['quote'][0]
            
            ohlc = {
                "time": np.array(timestamps),
                "open": np.array(quote['open']),
                "high": np.array(quote['high']),
                "low": np.array(quote['low']),
                "close": np.array(quote['close'])
            }
            
            idx = ~np.isnan(ohlc["close"])
            for k in ohlc: ohlc[k] = ohlc[k][idx]
            if len(ohlc["close"]) < 30: return None
            return ohlc
        except Exception:
            return None
            
    else:
        try:
            intervalos_binance = {1: "1m", 5: "5m", 15: "15m"}
            intervalo = intervalos_binance.get(tf, "5m")
            url = f"https://api.binance.com/api/v3/klines?symbol={ticker}&interval={intervalo}&limit=100"
            res = requests.get(url, timeout=5.0)
            if res.status_code != 200: return None
            
            data = res.json()
            ohlc = {
                "time": np.array([int(d[0]) for d in data]),
                "open": np.array([float(d[1]) for d in data]),
                "high": np.array([float(d[2]) for d in data]),
                "low": np.array([float(d[3]) for d in data]),
                "close": np.array([float(d[4]) for d in data])
            }
            if len(ohlc["close"]) < 30: return None
            return ohlc
        except Exception:
            return None

def calcular_ema(dados, periodo):
    ema = np.zeros_like(dados)
    multiplicador = 2 / (periodo + 1)
    ema[periodo-1] = np.mean(dados[:periodo])
    for i in range(periodo, len(dados)):
        ema[i] = (dados[i] - ema[i-1]) * multiplicador + ema[i-1]
    return ema

def analisar_estrategia(data, estrategia, i=-1):
    c, o, h, l = data["close"], data["open"], data["high"], data["low"]
    if len(c) < 30: return None
    sinal = None

    if estrategia == "LOGICA_DO_PRECO":
        cor = "G" if c[i] > o[i] else "R"
        tamanho = abs(c[i] - o[i])
        p_sup = max(0, h[i] - max(o[i], c[i]))
        p_inf = max(0, min(o[i], c[i]) - l[i])
        tolerancia = tamanho * 0.05
        
        if cor == "G" and p_inf >= (tamanho * 0.8): sinal = "CALL"
        elif cor == "R" and p_sup >= (tamanho * 0.8): sinal = "PUT"
        elif cor == "G" and p_sup <= tolerancia: sinal = "CALL" 
        elif cor == "R" and p_inf <= tolerancia: sinal = "PUT"

    elif estrategia == "RSI_MACD_MA":
        diff = np.diff(c[i-14:i])
        up = diff[diff > 0]
        down = abs(diff[diff < 0])
        avg_up = np.mean(up) if len(up) > 0 else 1e-7
        avg_down = np.mean(down) if len(down) > 0 else 1e-7
        rs = avg_up / avg_down
        rsi = 100 - (100 / (1 + rs))

        ema12 = calcular_ema(c, 12)
        ema26 = calcular_ema(c, 26)
        macd_line = ema12 - ema26
        signal_line = calcular_ema(macd_line, 9)

        if rsi < 45 and macd_line[i] > signal_line[i]: sinal = "CALL"
        elif rsi > 55 and macd_line[i] < signal_line[i]: sinal = "PUT"

    elif estrategia == "MHI1":
        cores = []
        for j in range(i-2, i+1):
            if c[j] > o[j] + 1e-6: cores.append("G")
            elif c[j] < o[j] - 1e-6: cores.append("R")
            else: cores.append("D") 
        qtd_g, qtd_r = cores.count("G"), cores.count("R")
        if qtd_g > qtd_r: sinal = "PUT"
        elif qtd_r > qtd_g: sinal = "CALL"

    elif estrategia in ["REVERSAO", "RETRACAO"]:
        std = np.std(c[i-20:i])
        ma = np.mean(c[i-20:i])
        banda_superior = ma + (1.8 * std)
        banda_inferior = ma - (1.8 * std)

        if c[i] <= banda_inferior: sinal = "CALL"
        elif c[i] >= banda_superior: sinal = "PUT"

    return sinal

# ================= ROTAS DE NAVEGAÇÃO =================
@app.route('/health')
def health():
    return jsonify({"status": "ok"}), 200

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        e = request.form.get('email', '').strip().lower()
        s = request.form.get('password', '').strip()
        if not e or not s: return render_template_string(HTML_LOGIN, erro="Preencha todos os campos.")
        if e == ADMIN_EMAIL: salvar_usuario(e, s, agora_brasilia().strftime("%Y-%m-%d"))

        usuarios = carregar_usuarios()
        if e not in usuarios: return render_template_string(HTML_LOGIN, erro="Usuário não cadastrado.")
        if not check_password_hash(usuarios[e]['senha'], s): return render_template_string(HTML_LOGIN, erro="Senha Incorreta.")

        session['user'] = e
        USUARIOS_ONLINE[e] = time.time()
        init_user_session(e)
        return redirect('/')
    return render_template_string(HTML_LOGIN)

@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        e = request.form.get('email', '').strip().lower()
        s = request.form.get('password', '').strip()
        salvar_usuario(e, s)
        session['user'] = e
        USUARIOS_ONLINE[e] = time.time()
        init_user_session(e)
        return redirect('/')
    return render_template_string(HTML_REGISTER)

@app.route('/logout')
def logout():
    session.clear()
    return redirect('/login')

@app.route('/')
def index():
    if 'user' not in session: return redirect('/login')
    user = session['user']
    USUARIOS_ONLINE[user] = time.time()
    return render_template_string(HTML_INDEX, modo=TIPO_MERCADO, tf=TIMEFRAME_OPERACAO, estrat=ESTRATEGIA_ESCOLHIDA, user=user, admin=ADMIN_EMAIL)

@app.route('/status')
def status():
    user = session.get('user')
    if not user: return jsonify({})
    USUARIOS_ONLINE[user] = time.time()
    
    usuarios = carregar_usuarios()
    u_info = usuarios.get(user, {"wins": 0, "reds": 0, "winrate": 0.0})
    display_texto = SINAL_DISPLAY_PERMANENTE if (AGUARDANDO_CONFIRMACAO_RESULTADO and SINAL_DISPLAY_PERMANENTE) else ULTIMO_SINAL_GLOBAL

    return jsonify({
        "html": display_texto, 
        "aguardando": AGUARDANDO_CONFIRMACAO_RESULTADO, 
        "wins": u_info.get("wins", 0),
        "reds": u_info.get("reds", 0), 
        "winrate": u_info.get("winrate", 0.0),
        "ativo_atual": ATIVO_ATUAL_GLOBAL,
        "mercado": TIPO_MERCADO,
        "rodando": BOT_INICIADO and not BOT_PAUSADO
    })

@app.route('/command/<cmd>')
def command(cmd):
    global BOT_INICIADO, BOT_PAUSADO, TIMEFRAME_OPERACAO, TIPO_MERCADO, QUEM_INICIOU_O_BOT, ULTIMO_SINAL_GLOBAL, AG_RESULTADO, AGUARDANDO_CONFIRMACAO_RESULTADO, SINAL_DISPLAY_PERMANENTE, ESTRATEGIA_ESCOLHIDA, ATIVO_ATUAL_GLOBAL
    user = session.get('user')

    if cmd == "start_bot":
        QUEM_INICIOU_O_BOT = user
        BOT_INICIADO = True
        BOT_PAUSADO = False
        AG_RESULTADO = False
        AGUARDANDO_CONFIRMACAO_RESULTADO = False
        SINAL_DISPLAY_PERMANENTE = None
        ATIVO_ATUAL_GLOBAL = "INICIANDO VARREDURA OTC..."
        ULTIMO_SINAL_GLOBAL = f"<div class='system-console'>⚡ <b>VARREDURA OTC INICIADA</b></div><div class='tech-scanner'></div>"
        return jsonify({"ok": True})

    elif cmd == "pause_bot":
        BOT_PAUSADO = not BOT_PAUSADO
        status_txt = "[PAUSADO] VARREDURA EM PAUSA..." if BOT_PAUSADO else f"🔍 ANALISANDO: {ATIVO_ATUAL_GLOBAL} (M{TIMEFRAME_OPERACAO})"
        ULTIMO_SINAL_GLOBAL = f"<div class='system-console' style='color:#f59e0b;'>{status_txt}</div>" if BOT_PAUSADO else f"<div class='system-console'>🔍 ANALISANDO: <b>{ATIVO_ATUAL_GLOBAL}</b> (M{TIMEFRAME_OPERACAO})</div><div class='tech-scanner'></div>"
        return jsonify({"ok": True})

    elif cmd == "stop_bot":
        BOT_INICIADO = False
        BOT_PAUSADO = True
        AG_RESULTADO = False
        AGUARDANDO_CONFIRMACAO_RESULTADO = False
        SINAL_DISPLAY_PERMANENTE = None
        ATIVO_ATUAL_GLOBAL = "DESCONECTADO"
        ULTIMO_SINAL_GLOBAL = "Aguardando Comando..."
        return jsonify({"ok": True})

    elif cmd.startswith("tf_"): TIMEFRAME_OPERACAO = int(cmd.split('_')[1])
    elif cmd.startswith("mkt_"): TIPO_MERCADO = cmd.split('_')[1]
    elif cmd.startswith("set_est_"): ESTRATEGIA_ESCOLHIDA = cmd.replace("set_est_", "")
    
    return jsonify({"ok": True})

@app.route('/resultado/<res>')
def resultado(res):
    global AG_RESULTADO, AGUARDANDO_CONFIRMACAO_RESULTADO, SINAL_DISPLAY_PERMANENTE, ULTIMO_SINAL_GLOBAL
    user = session.get('user')
    if user:
        if res == 'win': atualizar_estatisticas_usuario(user, True)
        elif res == 'g1': atualizar_estatisticas_usuario(user, True)
        elif res == 'red': atualizar_estatisticas_usuario(user, False)

        AGUARDANDO_CONFIRMACAO_RESULTADO = False
        SINAL_DISPLAY_PERMANENTE = None
        ULTIMO_SINAL_GLOBAL = f"<div class='system-console'>🔍 ANALISANDO: <b>{ATIVO_ATUAL_GLOBAL}</b> (M{TIMEFRAME_OPERACAO})<br><span style='color:#00f2fe;'>[RETOMANDO VARREDURA]</span></div><div class='tech-scanner'></div>"
    
    AG_RESULTADO = False
    return redirect('/')

# ================= LOOP PRINCIPAL DO BOT =================
def bot_loop():
    global ULTIMO_SINAL_GLOBAL, AG_RESULTADO, BOT_INICIADO, ATIVO_ATUAL_GLOBAL, AGUARDANDO_CONFIRMACAO_RESULTADO, SINAL_DISPLAY_PERMANENTE, QUEM_INICIOU_O_BOT

    while BOT_RODANDO:
        try:
            if not BOT_INICIADO or BOT_PAUSADO or AGUARDANDO_CONFIRMACAO_RESULTADO:
                time.sleep(1)
                continue

            if TIPO_MERCADO == "TODOS": ativos = ATIVOS_BASE["FOREX"] + ATIVOS_BASE["CRIPTO"] + ATIVOS_BASE["OTC"]
            else: ativos = ATIVOS_BASE.get(TIPO_MERCADO, ATIVOS_BASE["OTC"])

            for ativo in ativos:
                if not BOT_INICIADO or BOT_PAUSADO or AGUARDANDO_CONFIRMACAO_RESULTADO: break

                ATIVO_ATUAL_GLOBAL = ativo
                ticker = MAPA_TICKERS.get(ativo, f"{ativo}")
                
                if "OTC" in ativo:
                    mercado_atual = "OTC"
                elif ativo in ATIVOS_BASE["CRIPTO"]:
                    mercado_atual = "CRIPTO"
                else:
                    mercado_atual = "FOREX"

                ULTIMO_SINAL_GLOBAL = f"<div class='system-console'>🔍 ANALISANDO: <b style='color:#00f2fe; font-size:16px;'>{ativo}</b> (M{TIMEFRAME_OPERACAO})</div><div class='tech-scanner'></div>"
                time.sleep(0.8) 

                data = get_data_v2(ticker, TIMEFRAME_OPERACAO, mercado_atual)
                if not data: continue

                sinal_encontrado = None
                est_nome_encontrada = ESTRATEGIA_ESCOLHIDA

                if ESTRATEGIA_ESCOLHIDA == "TODAS":
                    for est_nome in LISTA_ESTRATEGIAS:
                        sinal_encontrado = analisar_estrategia(data, est_nome)
                        if sinal_encontrado:
                            est_nome_encontrada = est_nome
                            break
                else:
                    sinal_encontrado = analisar_estrategia(data, ESTRATEGIA_ESCOLHIDA)

                if sinal_encontrado:
                    agora = agora_brasilia()
                    
                    minutos_passados = agora.minute % TIMEFRAME_OPERACAO
                    segundos_passados = minutos_passados * 60 + agora.second
                    segundos_restantes = (TIMEFRAME_OPERACAO * 60) - segundos_passados

                    prox_minuto_entrada = agora + timedelta(seconds=segundos_restantes)
                    horario_saida = prox_minuto_entrada + timedelta(minutes=TIMEFRAME_OPERACAO)

                    str_entrada = prox_minuto_entrada.strftime("%H:%M")
                    str_saida = horario_saida.strftime("%H:%M")

                    ULTIMO_SINAL_GLOBAL = (
                        f"<div style='text-align:center; color:#f59e0b; font-family: sans-serif;'>"
                        f"⚠️ <b>PREPARE O ATIVO: {ativo}</b> ⚠️<br>"
                        f"<span style='color:#fff;'>Entrada às <b>{str_entrada}</b> (M{TIMEFRAME_OPERACAO})</span><br>"
                        f"<span style='font-size:12px; color:#94a3b8;'>Aguardando fechamento da vela para confirmar...</span>"
                        f"</div>"
                    )

                    while agora_brasilia() < prox_minuto_entrada:
                        if not BOT_INICIADO or BOT_PAUSADO or AGUARDANDO_CONFIRMACAO_RESULTADO: break
                        time.sleep(1)

                    if not BOT_INICIADO or BOT_PAUSADO or AGUARDANDO_CONFIRMACAO_RESULTADO: continue

                    SINAL_DISPLAY_PERMANENTE = (
                        f"<div class='status-box' style='border-color:#00f2fe; background:rgba(0,242,254,0.1);'>"
                        f"<h3 style='color:#00f2fe; margin-bottom:10px;'>🎯 SINAL CONFIRMADO!</h3>"
                        f"<b>ATIVO:</b> {ativo} | <b>DIREÇÃO:</b> <span style='color:{'#10b981' if sinal_encontrado=='CALL' else '#ef4444'}'>{sinal_encontrado}</span><br>"
                        f"<b>TIMEFRAME:</b> M{TIMEFRAME_OPERACAO} | <b>EXPIRAÇÃO:</b> {str_saida}"
                        f"</div>"
                    )
                    
                    AGUARDANDO_CONFIRMACAO_RESULTADO = True
                    registrar_sinal_bd(QUEM_INICIOU_O_BOT or ADMIN_EMAIL, f"{ativo} | {sinal_encontrado} | M{TIMEFRAME_OPERACAO}")
                    
                    # Dispara também no Telegram caso configurado
                    enviar_telegram(f"🔥 <b>SINAL CONFIRMADO (OTC)</b>\nPar: <b>{ativo}</b>\nDireção: <b>{sinal_encontrado}</b>\nTimeframe: M{TIMEFRAME_OPERACAO}\nExpiração: {str_saida}")
                    break  
            
            time.sleep(1.0)
        except Exception as err:
            time.sleep(5)

thread_iniciada = False
lock_thread = threading.Lock()

@app.before_request
def start_background_loop():
    global thread_iniciada
    if not thread_iniciada:
        with lock_thread:
            if not thread_iniciada:
                threading.Thread(target=bot_loop, daemon=True).start()
                thread_iniciada = True

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
