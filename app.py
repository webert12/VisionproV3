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
from datetime import datetime
from flask import Flask, render_template_string, request, jsonify, session, redirect, abort
from werkzeug.security import generate_password_hash, check_password_hash
import psycopg2
from psycopg2.extras import RealDictCursor

# ================= CONFIGURAÇÕES DE AMBIENTE =================
TOKEN_TELEGRAM = os.getenv("TOKEN_TELEGRAM", "")
CHAT_ID_TELEGRAM = os.getenv("CHAT_ID_TELEGRAM", "")
ADMIN_EMAIL = os.getenv("ADMIN_EMAIL", "admin@vision.com").strip().lower()

DB_URL = os.getenv("DB_URL") or os.getenv("DATABASE_URL", "").strip()

def get_db_connection():
    """Cria e retorna uma conexão direta via DB_URL."""
    if not DB_URL:
        raise ValueError("A variável de ambiente DB_URL (ou DATABASE_URL) precisa estar configurada.")
    return psycopg2.connect(DB_URL)

USUARIOS_ONLINE = {}
DADOS_USUARIOS = {}

ULTIMO_MSG_ID_TELEGRAM = None
QUEM_INICIOU_O_BOT = None

def enviar_telegram(mensagem, auto_delete=None):
    global ULTIMO_MSG_ID_TELEGRAM
    if not TOKEN_TELEGRAM or not CHAT_ID_TELEGRAM:
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
        .green { background: #10b981; } .red { background: #ef4444; } .blue { background: #3b82f6; }
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
                <span style="color:#94a3b8;">Wins: {{ info.wins }} | Reds: {{ info.reds }}</span>
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
    body { background: #0a0f1d; color: white; font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; padding: 20px; line-height: 1.6; }
    .card { background: #0f172a; padding: 25px; border-radius: 15px; max-width: 600px; margin: auto; border: 1px solid #00f2fe; box-shadow: 0 0 20px rgba(0,242,254,0.15); }
    h2 { color: #00f2fe; border-bottom: 1px solid #1e293b; padding-bottom: 10px; text-transform: uppercase; }
    p { font-size: 14px; color: #94a3b8; }
    .btn { display: block; text-align: center; background: linear-gradient(135deg, #00c6ff, #0072ff); color: white; padding: 14px; border-radius: 8px; text-decoration: none; font-weight: bold; margin-top: 20px; box-shadow: 0 4px 15px rgba(0,198,255,0.4); }
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
        
        .container {
            width: 100%;
            max-width: 520px;
            background: rgba(15, 23, 42, 0.8);
            border: 1px solid rgba(0, 242, 254, 0.2);
            border-radius: 24px;
            padding: 20px;
            box-shadow: 0 20px 50px rgba(0, 0, 0, 0.8), 0 0 20px rgba(0, 242, 254, 0.05);
            backdrop-filter: blur(12px);
        }

        .header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 18px; padding-bottom: 14px; border-bottom: 1px solid rgba(255, 255, 255, 0.08); }
        .brand { font-size: 17px; font-weight: 900; letter-spacing: 1px; color: #00f2fe; display: flex; align-items: center; gap: 8px; text-shadow: 0 0 10px rgba(0,242,254,0.4); }
        .brand span { background: rgba(0, 242, 254, 0.15); color: #38ef7d; font-size: 10px; padding: 3px 8px; border-radius: 12px; border: 1px solid rgba(56, 239, 125, 0.4); font-weight: 700; }
        .btn-logout { font-size: 12px; color: #ef4444; text-decoration: none; font-weight: 700; padding: 6px 14px; border-radius: 10px; background: rgba(239, 68, 68, 0.1); border: 1px solid rgba(239, 68, 68, 0.25); transition: 0.2s; }
        .btn-logout:hover { background: rgba(239, 68, 68, 0.2); }

        .placar-card { background: #0b1120; border: 1px solid #1e293b; border-radius: 16px; padding: 16px; margin-bottom: 16px; box-shadow: inset 0 2px 4px rgba(0,0,0,0.5); }
        .placar-grid { display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 10px; text-align: center; }
        .placar-item .title { font-size: 11px; text-transform: uppercase; color: #64748b; font-weight: 700; margin-bottom: 4px; letter-spacing: 0.5px; }
        .placar-item .val { font-size: 20px; font-weight: 800; font-family: 'JetBrains Mono', monospace; }
        .win-color { color: #10b981; text-shadow: 0 0 10px rgba(16,185,129,0.3); }
        .loss-color { color: #ef4444; text-shadow: 0 0 10px rgba(239,68,68,0.3); }
        .wr-color { color: #3b82f6; text-shadow: 0 0 10px rgba(59,130,246,0.3); }
        .winrate-bar { height: 6px; background: #1e293b; border-radius: 10px; overflow: hidden; margin-top: 14px; }
        .winrate-fill { height: 100%; background: linear-gradient(90deg, #059669, #10b981); width: 0%; transition: width 0.5s ease-in-out; }

        /* EMBED DA CORRETORA (OCULTO POR PADRÃO) */
        #broker-view-container { display: none; width: 100%; height: 350px; border-radius: 16px; overflow: hidden; flex-direction: column; margin-bottom: 16px; background: #0b1120; border: 1px solid #1e293b; padding: 8px; }
        .broker-iframe-inline { width: 100%; height: 100%; border: none; background: #0b1120; border-radius: 10px; }
        .btn-close-broker { background: #1e293b; border: 1px solid #334155; color: #00f2fe; padding: 6px 12px; font-size: 11px; font-weight: 700; border-radius: 6px; cursor: pointer; margin-bottom: 8px; width: 100%; text-align: center; }

        .status-box { background: linear-gradient(145deg, #0f172a, #0b1120); border: 1px solid rgba(0, 242, 254, 0.3); padding: 18px; border-radius: 16px; margin-bottom: 16px; min-height: 90px; text-align: center; display: flex; flex-direction: column; align-items: center; justify-content: center; font-size: 14px; font-weight: 600; box-shadow: inset 0 2px 4px rgba(0,0,0,0.6), 0 0 15px rgba(0, 242, 254, 0.08); }
        
        .system-console { font-family: 'JetBrains Mono', monospace; color: #38ef7d; font-size: 13px; text-shadow: 0 0 5px rgba(56, 239, 125, 0.5); width: 100%; }

        .result-grid { display: grid; grid-template-columns: repeat(4, 1fr); gap: 8px; margin-bottom: 16px; }
        .btn-res { border: none; padding: 12px; border-radius: 10px; font-weight: 800; font-size: 12px; cursor: pointer; color: white; transition: transform 0.1s, box-shadow 0.2s; text-transform: uppercase; }
        .btn-res:active { transform: scale(0.95); }
        .btn-res-win { background: linear-gradient(135deg, #10b981, #059669); box-shadow: 0 4px 12px rgba(16,185,129,0.3); }
        .btn-res-g1 { background: linear-gradient(135deg, #f59e0b, #d97706); color: #000; box-shadow: 0 4px 12px rgba(245,158,11,0.3); }
        .btn-res-red { background: linear-gradient(135deg, #ef4444, #dc2626); box-shadow: 0 4px 12px rgba(239,68,68,0.3); }
        .btn-res-skip { background: #334155; box-shadow: 0 4px 12px rgba(51,65,85,0.3); }

        .section-label { font-size: 11px; font-weight: 800; color: #64748b; text-transform: uppercase; margin-bottom: 10px; letter-spacing: 1px; }
        .menu-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; margin-bottom: 12px; }
        .sub-menu { display: none; grid-template-columns: repeat(3, 1fr); gap: 8px; padding: 14px; background: #0b1120; border: 1px solid #1e293b; border-radius: 14px; margin-bottom: 16px; }
        
        button.btn-menu { background: #0f172a; border: 1px solid #1e293b; color: #cbd5e1; padding: 12px; border-radius: 12px; font-weight: 700; font-size: 12px; display: flex; align-items: center; justify-content: center; gap: 6px; cursor: pointer; transition: 0.2s; box-shadow: 0 2px 5px rgba(0,0,0,0.2); }
        button.btn-menu:hover { background: #1e293b; border-color: #334155; color: #fff; }
        
        .btn-opt { padding: 10px; font-size: 11px; background: #0f172a; border: 1px solid #1e293b; color: #94a3b8; border-radius: 10px; font-weight: 700; cursor: pointer; transition: 0.2s; }
        .btn-opt:hover { color: #fff; border-color: #00f2fe; }
        .btn-active { background: linear-gradient(135deg, #00c6ff, #0072ff) !important; color: #ffffff !important; border-color: #00f2fe !important; box-shadow: 0 0 12px rgba(0, 198, 255, 0.4); }

        /* Estilo dos Botões das Corretoras */
        .broker-grid { display: none; grid-template-columns: 1fr 1fr; gap: 10px; margin-bottom: 16px; padding: 12px; background: #0b1120; border: 1px solid #1e293b; border-radius: 14px; }
        .btn-broker { border: 1px solid #1e293b; background: #0f172a; color: #fff; padding: 12px; border-radius: 12px; font-weight: 800; font-size: 12px; cursor: pointer; transition: 0.3s; display: flex; align-items: center; justify-content: center; gap: 8px; }
        .btn-broker:hover { transform: translateY(-2px); border-color: #00f2fe; box-shadow: 0 4px 15px rgba(0, 242, 254, 0.2); }

        .historico-box { display: none; background: #0b1120; border: 1px solid #1e293b; border-radius: 14px; padding: 12px; margin-bottom: 16px; }
        .historico-scroll { max-height: 160px; overflow-y: auto; }
        .historico-item { font-size: 12px; padding: 8px 0; border-bottom: 1px solid rgba(255,255,255,0.05); display: flex; justify-content: space-between; align-items: center; font-family: 'JetBrains Mono', monospace; }
        .historico-item:last-child { border-bottom: none; }

        .tech-scanner { width: 26px; height: 26px; margin: 8px auto 0; border: 3px solid rgba(0, 242, 254, 0.2); border-top-color: #00f2fe; border-radius: 50%; animation: spin 0.8s linear infinite; }
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
                <div class="placar-item">
                    <div class="title">WINS</div>
                    <div class="val win-color" id="win-count">0</div>
                </div>
                <div class="placar-item">
                    <div class="title">ASSERTIVIDADE</div>
                    <div class="val wr-color" id="wr-text">0%</div>
                </div>
                <div class="placar-item">
                    <div class="title">LOSS</div>
                    <div class="val loss-color" id="loss-count">0</div>
                </div>
            </div>
            <div class="winrate-bar"><div id="wr-fill" class="winrate-fill"></div></div>
        </div>

        <!-- VISUALIZADOR DA CORRETORA INTEGRADO -->
        <div id="broker-view-container">
            <button class="btn-close-broker" onclick="closeBrokerView()">❌ FECHAR CORRETORA</button>
            <iframe id="brokerIframe" class="broker-iframe-inline" src=""></iframe>
        </div>

        <div class="status-box" id="panel-text">Aguardando Comando...</div>

        <div id="result-area" class="result-grid" style="display:none;">
            <button class="btn-res btn-res-win" onclick="fetch('/resultado/win')">WIN</button>
            <button class="btn-res btn-res-g1" onclick="fetch('/resultado/g1')">G1</button>
            <button class="btn-res btn-res-red" onclick="fetch('/resultado/red')">RED</button>
            <button class="btn-res btn-res-skip" onclick="fetch('/resultado/pular')">PULAR</button>
        </div>

        <!-- PAINEL DE CONTROLE DO BOT -->
        <div class="menu-grid">
            <button class="btn-menu" onclick="toggleSub('menu-brokers')" style="grid-column: span 2; border-color: rgba(0, 242, 254, 0.3);">🌐 PLATAFORMAS DE OPERAÇÃO (CORRETORAS)</button>
            
            <div id="menu-brokers" class="broker-grid" style="grid-column: span 2;">
                <button class="btn-broker" onclick="openBroker('https://qxbroker.com')">🌐 QUOTEX</button>
                <button class="btn-broker" onclick="openBroker('https://iqoption.com')">📈 IQ OPTION</button>
                <button class="btn-broker" onclick="openBroker('https://binomo.com')">🟡 BINOMO</button>
                <button class="btn-broker" onclick="openBroker('https://pocketoption.com')">🟦 POCKET OPTION</button>
            </div>

            <button class="btn-menu" onclick="toggleSub('menu-inicio')">⚡ SISTEMA BOT</button>
            <button class="btn-menu" onclick="toggleSub('menu-historico')">📊 HISTÓRICO</button>
            <button class="btn-menu" onclick="toggleSub('menu-mercado')">🌐 MERCADO</button>
            <button class="btn-menu" onclick="toggleSub('menu-times')">⏱️ TIMEFRAME</button>
            <button class="btn-menu" onclick="toggleSub('menu-estrategias')" style="grid-column: span 2;">⚙️ ESTRATÉGIAS OPERACIONAIS</button>
            {% if user == admin %}
            <button class="btn-menu" onclick="location.href='/admin_panel'" style="grid-column: span 2; background: rgba(0, 242, 254, 0.1); border-color: #00f2fe; color: #00f2fe;">🛡️ PAINEL ADMINISTRATIVO</button>
            {% endif %}
        </div>

        <div id="menu-historico" class="historico-box">
            <div class="historico-scroll" id="lista-sinais"></div>
        </div>
        
        <div id="menu-inicio" class="sub-menu">
            <button class="btn-opt" style="background: linear-gradient(135deg, #10b981, #059669); color:white;" onclick="sendCommand('start_bot')">ANALISAR / START</button>
            <button class="btn-opt" style="background: linear-gradient(135deg, #f59e0b, #d97706); color:#000;" onclick="sendCommand('pause_bot')">PAUSAR</button>
            <button class="btn-opt" style="background: linear-gradient(135deg, #ef4444, #dc2626); color:white;" onclick="sendCommand('stop_bot')">PARAR</button>
        </div>
        
        <div id="menu-mercado" class="sub-menu">
            <button class="btn-opt {{ 'btn-active' if modo == 'FOREX' }}" onclick="sendCommand('mkt_FOREX', this)">FOREX</button>
            <button class="btn-opt {{ 'btn-active' if modo == 'CRIPTO' }}" onclick="sendCommand('mkt_CRIPTO', this)">CRIPTO</button>
            <button class="btn-opt {{ 'btn-active' if modo == 'TODOS' }}" onclick="sendCommand('mkt_TODOS', this)">TODOS</button>
        </div>
        
        <div id="menu-times" class="sub-menu">
            <button class="btn-opt {{ 'btn-active' if tf == 1 }}" onclick="sendCommand('tf_1', this)">M1</button>
            <button class="btn-opt {{ 'btn-active' if tf == 5 }}" onclick="sendCommand('tf_5', this)">M5</button>
            <button class="btn-opt {{ 'btn-active' if tf == 15 }}" onclick="sendCommand('tf_15', this)">M15</button>
        </div>
        
        <div id="menu-estrategias" class="sub-menu" style="grid-template-columns: 1fr 1fr;">
            <button class="btn-opt {{ 'btn-active' if estrat == 'TODAS' }}" onclick="sendCommand('set_est_TODAS', this)">💎 TODAS (AUTO)</button>
            <button class="btn-opt {{ 'btn-active' if estrat == 'LOGICA_DO_PRECO' }}" onclick="sendCommand('set_est_LOGICA_DO_PRECO', this)">LÓGICA DO PREÇO</button>
            <button class="btn-opt {{ 'btn-active' if estrat == 'RSI_MACD_MA' }}" onclick="sendCommand('set_est_RSI_MACD_MA', this)">RSI + MACD + MA</button>
            <button class="btn-opt {{ 'btn-active' if estrat == 'MHI1' }}" onclick="sendCommand('set_est_MHI1', this)">MHI 1</button>
            <button class="btn-opt {{ 'btn-active' if estrat == 'REVERSAO' }}" onclick="sendCommand('set_est_REVERSAO', this)">REVERSÃO / RETRAÇÃO</button>
        </div>
    </div>

    <script>
        function openBroker(url) {
            const brokerContainer = document.getElementById('broker-view-container');
            document.getElementById('brokerIframe').src = url;
            brokerContainer.style.display = 'flex';
            document.getElementById('menu-brokers').style.display = 'none';
        }

        function closeBrokerView() {
            document.getElementById('broker-view-container').style.display = 'none';
            document.getElementById('brokerIframe').src = '';
        }

        function toggleSub(id) {
            ['menu-inicio', 'menu-mercado', 'menu-times', 'menu-historico', 'menu-estrategias', 'menu-brokers'].forEach(m => {
                const el = document.getElementById(m);
                if(el) el.style.display = (m === id && el.style.display !== 'grid' && el.style.display !== 'block') ? (id === 'menu-historico' ? 'block' : 'grid') : 'none';
            });
        }

        function sendCommand(cmd, el = null) {
            fetch('/command/' + cmd).then(r => r.json()).then(data => {
                if(data.redirect) window.location.href = data.redirect;
                if(el) {
                    const parent = el.parentElement;
                    if(parent) {
                        parent.querySelectorAll('.btn-opt').forEach(btn => btn.classList.remove('btn-active'));
                        el.classList.add('btn-active');
                    }
                }
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
                
                let histHtml = "";
                if(data.historico) {
                    data.historico.forEach(item => {
                        let cor = "#64748b";
                        if(item.res.includes("Win")) cor = "#10b981";
                        if(item.res.includes("Red")) cor = "#ef4444";
                        histHtml += `<div class="historico-item"><span>🕒 ${item.sinal}</span><b style="color:${cor}">${item.res}</b></div>`;
                    });
                }
                if(document.getElementById('lista-sinais')) document.getElementById('lista-sinais').innerHTML = histHtml || "<div style='text-align:center; font-size:11px; color:#64748b;'>Nenhum sinal no histórico.</div>";
            });
        }, 1500);
    </script>
</body>
</html>
"""

# ================= BANCO DE DADOS =================
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
                dict_usuarios[email] = dict(u)
        return dict_usuarios
    except Exception as e:
        print(f"Erro Banco (carregar_usuarios): {e}")
        return {}

def salvar_usuario(email, senha, data=None):
    try:
        email_clean = email.strip().lower()
        data_criacao = data if data else datetime.now().strftime("%Y-%m-%d")
        senha_hash = senha if senha.startswith("scrypt:") or senha.startswith("pbkdf2:") else generate_password_hash(senha)

        conn = get_db_connection()
        cur = conn.cursor()
        query = """
            INSERT INTO usuarios (email, senha, criado_em, wins, reds, winrate)
            VALUES (%s, %s, %s, 0, 0, 0.0)
            ON CONFLICT (email) DO UPDATE 
            SET senha = EXCLUDED.senha;
        """
        cur.execute(query, (email_clean, senha_hash, data_criacao))
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        print(f"Erro Banco (salvar_usuario): {e}")
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
        print(f"Erro Banco (atualizar_estatisticas): {e}")

def renovar_usuario_db(email):
    try:
        hoje = datetime.now().strftime("%Y-%m-%d")
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("UPDATE usuarios SET criado_em = %s WHERE email = %s;", (hoje, email.strip().lower()))
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        print(f"Erro Banco (renovar_usuario): {e}")

def excluir_usuario_db(email):
    try:
        email_clean = email.strip().lower()
        if email_clean != ADMIN_EMAIL:
            conn = get_db_connection()
            cur = conn.cursor()
            cur.execute("DELETE FROM usuarios WHERE email = %s;", (email_clean,))
            conn.commit()
            cur.close()
            conn.close()
    except Exception as e:
        print(f"Erro Banco (excluir_usuario): {e}")

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
        dias_restantes = 30 - (datetime.now() - data_criacao).days
        return (True, dias_restantes) if dias_restantes > 0 else (False, 0)
    except Exception as e:
        print(f"Erro Assinatura: {e}")
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
        print(f"Erro Banco (registrar_sinal): {e}")

def buscar_historico_bd(email):
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("""
            SELECT id, sinal, resultado 
            FROM historico_sinais 
            WHERE user_email = %s 
            ORDER BY id DESC LIMIT 10;
        """, (email.strip().lower(),))
        res = cur.fetchall()
        cur.close()
        conn.close()
        return [{"id": r["id"], "sinal": r["sinal"], "res": r["resultado"]} for r in res]
    except Exception as e:
        print(f"Erro Banco (buscar_historico): {e}")
        return []

def atualizar_ultimo_sinal_bd(email, resultado):
    try:
        email_clean = email.strip().lower()
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("""
            SELECT id FROM historico_sinais 
            WHERE user_email = %s 
            ORDER BY id DESC LIMIT 1;
        """, (email_clean,))
        res = cur.fetchone()

        if res:
            ultimo_id = res["id"]
            cur.execute("""
                UPDATE historico_sinais 
                SET resultado = %s 
                WHERE id = %s;
            """, (resultado, ultimo_id))
            conn.commit()

        cur.close()
        conn.close()
    except Exception as e:
        print(f"Erro Banco (atualizar_ultimo_sinal): {e}")

# ================= BOT CONFIGS =================
TIMEFRAME_OPERACAO = 5
TIPO_MERCADO = "TODOS"
ESTRATEGIA_ESCOLHIDA = "TODAS"
LISTA_ESTRATEGIAS = ["LOGICA_DO_PRECO", "RSI_MACD_MA", "MHI1", "REVERSAO"]

BOT_RODANDO = True
BOT_PAUSADO = True
BOT_INICIADO = False
AG_RESULTADO = False
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
    """Requisicao resiliente ao Yahoo Finance evitando travamentos no Render."""
    try:
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}?interval={tf}m&range={period}"
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.5'
        }
        session_req = requests.Session()
        res = session_req.get(url, headers=headers, timeout=6).json()
        
        result = res['chart']['result'][0]
        timestamps = result['timestamp']
        quote = result['indicators']['quote'][0]
        
        ohlc = {
            "time": np.array(timestamps),
            "open": np.array(quote['open']),
            "high": np.array(quote['high']),
            "low": np.array(quote['low']),
            "close": np.array(quote['close']),
            "volume": np.array(quote['volume'])
        }
        idx = ~np.isnan(ohlc["close"])
        for k in ohlc: ohlc[k] = ohlc[k][idx]
        return ohlc
    except Exception as e:
        return None

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

# ================= ROTAS =================
@app.route('/health')
def health():
    return jsonify({"status": "ok"}), 200

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        e = request.form.get('email', '').strip().lower()
        s = request.form.get('password', '').strip()

        if not e or not s:
            return render_template_string(HTML_LOGIN, erro="Preencha todos os campos.")

        if e == ADMIN_EMAIL:
            try:
                salvar_usuario(e, s, datetime.now().strftime("%Y-%m-%d"))
            except Exception as err:
                return render_template_string(HTML_LOGIN, erro=f"Erro ao registrar ADM: {err}")

        usuarios = carregar_usuarios()

        if e not in usuarios:
            return render_template_string(HTML_LOGIN, erro=f"Usuário não cadastrado ({e}). Faça o cadastro.")

        user_db = usuarios[e]

        if not check_password_hash(user_db['senha'], s):
            return render_template_string(HTML_LOGIN, erro="Senha Incorreta.")

        ativo, dias = verificar_assinatura(e)
        if not ativo:
            return render_template_string(HTML_LOGIN, erro=f"Assinatura expirada (Dias: {dias}).")

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
        
        if not e or not s:
            return render_template_string(HTML_REGISTER, erro="Preencha todos os campos.")
            
        try:
            salvar_usuario(e, s)
            session['user'] = e
            USUARIOS_ONLINE[e] = time.time()
            init_user_session(e)
            return redirect('/')
        except Exception as err:
            return render_template_string(HTML_REGISTER, erro=f"Erro ao salvar: {err}")

    return render_template_string(HTML_REGISTER)

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
    original = request.form.get('email_original', '').strip().lower()
    novo_email = request.form.get('novo_email', '').strip().lower()
    nova_senha = request.form.get('nova_senha', '').strip()
    
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        if nova_senha:
            hash_senha = generate_password_hash(nova_senha)
            cur.execute("UPDATE usuarios SET email = %s, senha = %s WHERE email = %s;", (novo_email, hash_senha, original))
        else:
            cur.execute("UPDATE usuarios SET email = %s WHERE email = %s;", (novo_email, original))
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        print(f"Erro Editar Admin: {e}")
        
    return redirect('/admin_panel')

@app.route('/adm/excluir/<email>')
def adm_excluir(email):
    if session.get('user') != ADMIN_EMAIL: return abort(403)
    excluir_usuario_db(email)
    return redirect('/admin_panel')

@app.route('/')
def index():
    if 'user' not in session: return redirect('/login')
    user = session['user']
    USUARIOS_ONLINE[user] = time.time()
    init_user_session(user)
    return render_template_string(HTML_INDEX, modo=TIPO_MERCADO, tf=TIMEFRAME_OPERACAO, estrat=ESTRATEGIA_ESCOLHIDA, user=user, admin=ADMIN_EMAIL)

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
        BOT_INICIADO = True
        BOT_PAUSADO = False
        AG_RESULTADO = False
        ULTIMO_SINAL_GLOBAL = "<div class='system-console'>[VISION CORE] SERVIDORES CONECTADOS<br>[SCANNER] SCANNER EM TEMPO REAL INICIADO...</div><div class='tech-scanner'></div>"
        enviar_telegram(f"🚀 <b>SISTEMA VISION PRO V3 CONECTADO</b>\nSessão iniciada por {user}")
        return jsonify({"ok": True})

    elif cmd == "pause_bot":
        BOT_PAUSADO = not BOT_PAUSADO
        ULTIMO_SINAL_GLOBAL = "<div class='system-console' style='color:#f59e0b;'>[PAUSADO] VARREDURA EM PAUSA...</div>" if BOT_PAUSADO else "<div class='system-console'>[SCANNER] REINICIANDO VARREDURA...</div><div class='tech-scanner'></div>"
        return jsonify({"ok": True})

    elif cmd == "stop_bot":
        BOT_INICIADO = False
        BOT_PAUSADO = True
        ULTIMO_SINAL_GLOBAL = "Aguardando Início..."
        return jsonify({"ok": True, "redirect": "/login"})

    elif cmd.startswith("tf_"): 
        TIMEFRAME_OPERACAO = int(cmd.split('_')[1])
    elif cmd.startswith("mkt_"): 
        TIPO_MERCADO = cmd.split('_')[1]
    elif cmd.startswith("set_est_"): 
        ESTRATEGIA_ESCOLHIDA = cmd.replace("set_est_", "")
    
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

        ULTIMO_SINAL_GLOBAL = "<div class='system-console'>[SCANNER] BUSCANDO PRÓXIMA OPORTUNIDADE...</div><div class='tech-scanner'></div>"
    AG_RESULTADO = False
    return redirect('/')

# ================= LOOP DO BOT =================
def bot_loop():
    global ULTIMO_SINAL_GLOBAL, AG_RESULTADO, BOT_INICIADO, ATIVO_ATUAL_GLOBAL
    while BOT_RODANDO:
        try:
            if not BOT_INICIADO or BOT_PAUSADO or AG_RESULTADO:
                time.sleep(1)
                continue

            ativos = ATIVOS_BASE["FOREX"] + ATIVOS_BASE["CRIPTO"] if TIPO_MERCADO == "TODOS" else ATIVOS_BASE[TIPO_MERCADO]
            
            for ativo in ativos:
                if not BOT_INICIADO or BOT_PAUSADO or AG_RESULTADO:
                    break
                    
                ticker = MAPA_TICKERS.get(ativo, ativo)
                ATIVO_ATUAL_GLOBAL = ticker
                
                # Atualização visual da varredura
                ULTIMO_SINAL_GLOBAL = f"<div class='system-console'>🔍 ANALISANDO: <b>{ativo}</b> (M{TIMEFRAME_OPERACAO})<br><span style='color:#00f2fe;'>[SCANNER] PROCESSANDO VELAS E INDICADORES...</span></div><div class='tech-scanner'></div>"
                
                data = get_data_v2(ticker, TIMEFRAME_OPERACAO)
                if not data:
                    time.sleep(0.3)
                    continue

                sinal = None
                if ESTRATEGIA_ESCOLHIDA == "TODAS":
                    for est_nome in LISTA_ESTRATEGIAS:
                        sinal = analisar_estrategia(data, est_nome)
                        if sinal: break
                else:
                    sinal = analisar_estrategia(data, ESTRATEGIA_ESCOLHIDA)

                if sinal:
                    dir_txt = "COMPRA ⬆️" if sinal == "CALL" else "VENDA ⬇️"
                    ULTIMO_SINAL_GLOBAL = f"<div style='text-align:center;'>🎯 <b style='color:#00f2fe; font-size:16px;'>SINAL CONFIRMADO!</b><br><span style='font-size:18px; font-weight:800; color:#fff;'>{ativo}</span><br>Entrada: <b style='color:#38ef7d;'>{dir_txt}</b></div>"
                    
                    for u in list(USUARIOS_ONLINE.keys()):
                        registrar_sinal_bd(u, f"{ativo} (M{TIMEFRAME_OPERACAO})")
                        
                    enviar_telegram(f"🎯 <b>SINAL CONFIRMADO</b>\n\n📈 Ativo: {ativo}\n🕒 Timeframe: M{TIMEFRAME_OPERACAO}\n↕️ Direção: {dir_txt}")
                    AG_RESULTADO = True
                    break
                
                time.sleep(0.5)
            time.sleep(1)
        except Exception as e: 
            print(f"Erro Loop Bot: {e}")
            time.sleep(2)

# Thread em background
threading.Thread(target=bot_loop, daemon=True).start()

if __name__ == "__main__":
    app.run()
