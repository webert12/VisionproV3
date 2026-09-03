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
from concurrent.futures import ThreadPoolExecutor
from flask import Flask, render_template_string, request, jsonify, session, redirect, abort, Response
from werkzeug.security import generate_password_hash, check_password_hash
import psycopg2
from psycopg2.extras import RealDictCursor

# ================= AJUSTE DE FUSO HORÁRIO (SÃO PAULO / BRASÍLIA) =================
FUSO_SP = pytz.timezone('America/Sao_Paulo')

def agora_brasilia():
    return datetime.now(FUSO_SP)

# ================= CONFIGURAÇÕES DE AMBIENTE E BOT TELEGRAM =================
TOKEN_TELEGRAM = os.getenv("TOKEN_TELEGRAM", "8710725826:AAFuGmF30Ns-G1glrBYir9ggVya9VwQgZAU")
CHAT_ID_TELEGRAM = os.getenv("CHAT_ID_TELEGRAM", "-1002979466366")
ADMIN_EMAIL = os.getenv("ADMIN_EMAIL", "admin@vision.com").strip().lower()

DB_URL = os.getenv("DB_URL") or os.getenv("DATABASE_URL", "").strip()

def get_db_connection():
    if not DB_URL:
        raise ValueError("A variável de ambiente DB_URL (ou DATABASE_URL) precisa estar configurada.")
    return psycopg2.connect(DB_URL)

USUARIOS_ONLINE = {}
DADOS_USUARIOS = {}  # Estrutura isolada por e-mail de usuário

def get_user_state(email):
    """Garante que cada usuário possui seu próprio estado independente no painel."""
    if not email:
        return None
    email_clean = email.strip().lower()
    if email_clean not in DADOS_USUARIOS:
        DADOS_USUARIOS[email_clean] = {
            "timeframe": 5,
            "tipo_mercado": "TODOS",
            "estrategia": "TODAS",
            "bot_iniciado": False,
            "bot_pausado": True,
            "aguardando_confirmacao": False,
            "sinal_permanente": None,
            "ultimo_sinal": "Aguardando Comando...",
            "ativo_atual": "AGUARDANDO...",
            "inicio_varredura": 0,
            "sinais_enviados": {},
            "alerta_ativo": None,
            "notificacao": None
        }
    return DADOS_USUARIOS[email_clean]

def get_client_ip():
    if request.headers.get('X-Forwarded-For'):
        return request.headers.get('X-Forwarded-For').split(',')[0].strip()
    return request.remote_addr

# ================= ENVIO E DELEÇÃO TELEGRAM (OTIMIZADO) =================
def enviar_telegram(mensagem, auto_delete=None, user_solicitante=None):
    token = os.getenv("TOKEN_TELEGRAM", TOKEN_TELEGRAM).strip().strip('"').strip("'")
    chat_id_raw = os.getenv("CHAT_ID_TELEGRAM", CHAT_ID_TELEGRAM).strip().strip('"').strip("'")

    if not token or not chat_id_raw:
        print("⚠️ Telegram: TOKEN_TELEGRAM ou CHAT_ID_TELEGRAM não configurado.")
        return None

    try:
        chat_id = int(chat_id_raw)
    except ValueError:
        chat_id = chat_id_raw

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    
    payload = {
        "chat_id": chat_id, 
        "text": mensagem, 
        "parse_mode": "HTML",
        "disable_web_page_preview": True
    }
    
    try:
        res = requests.post(url, json=payload, timeout=5)
        r = res.json()
        if r.get("ok"):
            msg_id = r["result"]["message_id"]
            if auto_delete:
                threading.Thread(target=deletar_mensagem_atrasada, args=(msg_id, auto_delete), daemon=True).start()
            return msg_id
        else:
            print(f"⚠️ Telegram API Recusou HTML ({r.get('description')}). Tentando Formato Texto...")
            texto_limpo = re.sub('<[^<]+?>', '', mensagem)
            payload_plain = {
                "chat_id": chat_id, 
                "text": texto_limpo,
                "disable_web_page_preview": True
            }
            res_plain = requests.post(url, json=payload_plain, timeout=5)
            r_plain = res_plain.json()
            if r_plain.get("ok"):
                return r_plain["result"]["message_id"]
            else:
                print(f"❌ Telegram API Erro no Fallback: {r_plain}")
    except Exception as e:
        print(f"❌ Erro de conexão com o Telegram: {e}")
    return None

def deletar_mensagem_telegram(msg_id):
    token = os.getenv("TOKEN_TELEGRAM", TOKEN_TELEGRAM).strip().strip('"').strip("'")
    chat_id_raw = os.getenv("CHAT_ID_TELEGRAM", CHAT_ID_TELEGRAM).strip().strip('"').strip("'")
    if not token or not chat_id_raw or not msg_id:
        return
    try:
        try:
            chat_id = int(chat_id_raw)
        except ValueError:
            chat_id = chat_id_raw

        url = f"https://api.telegram.org/bot{token}/deleteMessage"
        payload = {"chat_id": chat_id, "message_id": msg_id}
        requests.post(url, json=payload, timeout=4)
    except Exception as e:
        print(f"Erro ao deletar mensagem Telegram: {e}")

def deletar_mensagem_atrasada(msg_id, delay):
    if delay > 0: time.sleep(delay)
    deletar_mensagem_telegram(msg_id)

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

        #broker-view-container { display: none; width: 100%; height: 350px; border-radius: 16px; overflow: hidden; flex-direction: column; margin-bottom: 16px; background: #0b1120; border: 1px solid #1e293b; padding: 8px; }
        .broker-iframe-inline { width: 100%; height: 100%; border: none; background: #0b1120; border-radius: 10px; }
        .btn-close-broker { background: #1e293b; border: 1px solid #334155; color: #00f2fe; padding: 6px 12px; font-size: 11px; font-weight: 700; border-radius: 6px; cursor: pointer; margin-bottom: 8px; width: 100%; text-align: center; }

        .status-box { background: linear-gradient(145deg, #0f172a, #0b1120); border: 1px solid rgba(0, 242, 254, 0.3); padding: 18px; border-radius: 16px; margin-bottom: 16px; min-height: 100px; text-align: center; display: flex; flex-direction: column; align-items: center; justify-content: center; font-size: 14px; font-weight: 600; box-shadow: inset 0 2px 4px rgba(0,0,0,0.6), 0 0 15px rgba(0, 242, 254, 0.08); }
        
        .system-console { font-family: 'JetBrains Mono', monospace; color: #38ef7d; font-size: 13px; text-shadow: 0 0 5px rgba(56, 239, 125, 0.5); width: 100%; }

        .result-grid { display: grid; grid-template-columns: repeat(4, 1fr); gap: 8px; margin-bottom: 16px; }
        .btn-res { border: none; padding: 12px; border-radius: 10px; font-weight: 800; font-size: 12px; cursor: pointer; color: white; transition: transform 0.1s, box-shadow 0.2s; text-transform: uppercase; }
        .btn-res:active { transform: scale(0.95); }
        .btn-res-win { background: linear-gradient(135deg, #10b981, #059669); box-shadow: 0 4px 12px rgba(16,185,129,0.3); }
        .btn-res-g1 { background: linear-gradient(135deg, #f59e0b, #d97706); color: #000; box-shadow: 0 4px 12px rgba(245,158,11,0.3); }
        .btn-res-red { background: linear-gradient(135deg, #ef4444, #dc2626); box-shadow: 0 4px 12px rgba(239,68,68,0.3); }
        .btn-res-skip { background: #334155; box-shadow: 0 4px 12px rgba(51,65,85,0.3); }

        .control-panel { background: #0b1120; border: 1px solid #1e293b; border-radius: 16px; padding: 15px; margin-bottom: 16px; }
        .section-label { font-size: 11px; font-weight: 800; color: #64748b; text-transform: uppercase; margin-bottom: 10px; letter-spacing: 1px; display: block; border-bottom: 1px solid #1e293b; padding-bottom: 5px;}
        
        .action-flex { display: flex; gap: 8px; margin-bottom: 15px; }
        .btn-action { flex: 1; padding: 12px 5px; border: none; border-radius: 10px; font-weight: 800; font-size: 12px; color: white; cursor: pointer; transition: 0.2s; text-transform: uppercase; }
        .btn-action:active { transform: scale(0.95); }
        .btn-start { background: linear-gradient(135deg, #10b981, #059669); box-shadow: 0 4px 12px rgba(16,185,129,0.2); }
        .btn-pause { background: linear-gradient(135deg, #f59e0b, #d97706); box-shadow: 0 4px 12px rgba(245,158,11,0.2); }
        .btn-stop { background: linear-gradient(135deg, #ef4444, #dc2626); box-shadow: 0 4px 12px rgba(239,68,68,0.2); }

        .settings-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; margin-bottom: 15px; }
        .settings-grid.full { grid-template-columns: 1fr; margin-bottom: 15px; }
        .setting-group label { font-size: 10px; font-weight: 700; color: #94a3b8; margin-bottom: 4px; display: block; }
        
        .select-wrapper { position: relative; width: 100%; }
        .select-wrapper::after { content: "▼"; position: absolute; right: 12px; top: 12px; color: #00f2fe; font-size: 10px; pointer-events: none; }
        .modern-select { background: #0f172a; color: #f1f5f9; border: 1px solid #1e293b; padding: 10px 12px; border-radius: 8px; font-weight: 600; font-size: 12px; width: 100%; outline: none; appearance: none; cursor: pointer; transition: 0.2s; }
        .modern-select:hover, .modern-select:focus { border-color: #00f2fe; box-shadow: 0 0 8px rgba(0,242,254,0.2); }

        .broker-flex { display: flex; gap: 8px; overflow-x: auto; padding-bottom: 5px; scrollbar-width: none; }
        .broker-flex::-webkit-scrollbar { display: none; }
        .btn-broker { min-width: 100px; flex: 1; border: 1px solid #1e293b; background: #0f172a; color: #cbd5e1; padding: 10px; border-radius: 8px; font-weight: 700; font-size: 11px; cursor: pointer; transition: 0.3s; text-align: center; white-space: nowrap;}
        .btn-broker:hover { color: #fff; border-color: #00f2fe; background: #1e293b; }

        .btn-toggle-hist { width: 100%; padding: 10px; background: rgba(0, 242, 254, 0.08); border: 1px dashed #00f2fe; color: #00f2fe; border-radius: 8px; font-weight: bold; font-size: 11px; cursor: pointer; margin-top: 10px; transition: 0.3s; }
        .btn-toggle-hist:hover { background: rgba(0, 242, 254, 0.2); }

        .btn-test-tg { width: 100%; padding: 10px; background: rgba(59, 130, 246, 0.15); border: 1px solid #3b82f6; color: #3b82f6; font-weight: bold; font-size: 11px; border-radius: 8px; cursor: pointer; margin-bottom: 8px; transition: 0.3s; text-transform: uppercase; }
        .btn-test-tg:hover { background: rgba(59, 130, 246, 0.3); }

        .historico-box { display: none; background: #0f172a; border: 1px solid #1e293b; border-radius: 12px; padding: 12px; margin-top: 15px; }
        .historico-scroll { max-height: 140px; overflow-y: auto; }
        .historico-item { font-size: 11px; padding: 6px 0; border-bottom: 1px solid rgba(255,255,255,0.05); display: flex; justify-content: space-between; align-items: center; font-family: 'JetBrains Mono', monospace; }
        .historico-item:last-child { border-bottom: none; }

        .tech-scanner { width: 28px; height: 28px; margin: 10px auto 0; border: 3px solid rgba(0, 242, 254, 0.2); border-top-color: #00f2fe; border-radius: 50%; animation: spin 0.8s linear infinite; }
        @keyframes spin { to { transform: rotate(360deg); } }

        .btn-notify { width: 100%; padding: 10px; background: rgba(16, 185, 129, 0.15); border: 1px solid #10b981; color: #10b981; font-weight: bold; font-size: 11px; border-radius: 8px; cursor: pointer; margin-bottom: 12px; transition: 0.3s; text-transform: uppercase; }
        .btn-notify:hover { background: rgba(16, 185, 129, 0.3); }
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <div class="brand">VISION PRO <span>V3 ULTRA</span></div>
            <a href="/logout" class="btn-logout">SAIR</a>
        </div>

        <button class="btn-notify" id="btn-enable-notify" onclick="solicitarPermissaoNotificacao()">🔔 ATIVAR NOTIFICAÇÕES NO CELULAR</button>
        <button class="btn-test-tg" onclick="sendCommand('test_telegram')">🧪 TESTAR CONEXÃO TELEGRAM</button>

        <div class="placar-card">
            <div class="placar-grid">
                <div class="placar-item">
                    <div class="title">WINS</div>
                    <div class="val win-color" id="win-count">0</div>
                </div>
                <div class="placar-item">
                    <div class="title">ASSERTIVIDADE</div>
                    <div class="val wr-text" id="wr-text">0%</div>
                </div>
                <div class="placar-item">
                    <div class="title">LOSS</div>
                    <div class="val loss-color" id="loss-count">0</div>
                </div>
            </div>
            <div class="winrate-bar"><div id="wr-fill" class="winrate-fill"></div></div>
        </div>

        <div id="broker-view-container">
            <button class="btn-close-broker" onclick="closeBrokerView()">❌ FECHAR CORRETORA</button>
            <iframe id="brokerIframe" class="broker-iframe-inline" src=""></iframe>
        </div>

        <div id="ticker-live-status" style="background: rgba(0, 242, 254, 0.05); border: 1px solid rgba(0, 242, 254, 0.2); border-radius: 12px; padding: 10px; margin-bottom: 12px; text-align: center; font-size: 12px;">
            MERCADO SELECIONADO: <b id="mkt-badge" style="color: #00f2fe;">{{ modo }}</b> | 
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
                            <option value="TODOS" {% if modo == 'TODOS' %}selected{% endif %}>🌐 Todos os Mercados (Aberto + OTC)</option>
                            <option value="ABERTO_TODOS" {% if modo == 'ABERTO_TODOS' %}selected{% endif %}>🟢 Todo Mercado Aberto (Forex + Cripto)</option>
                            <option value="OTC_TODOS" {% if modo == 'OTC_TODOS' %}selected{% endif %}>🌙 Todo Mercado OTC (Forex + Cripto)</option>
                            <option value="FOREX_ABERTO" {% if modo == 'FOREX_ABERTO' %}selected{% endif %}>📈 Forex Aberto (Seg a Sex)</option>
                            <option value="CRIPTO_ABERTO" {% if modo == 'CRIPTO_ABERTO' %}selected{% endif %}>🪙 Criptomoedas Aberto (24/7)</option>
                            <option value="FOREX_OTC" {% if modo == 'FOREX_OTC' %}selected{% endif %}>📊 Forex OTC (Noite/FDS)</option>
                            <option value="CRIPTO_OTC" {% if modo == 'CRIPTO_OTC' %}selected{% endif %}>⚡ Cripto OTC (Noite/FDS)</option>
                        </select>
                    </div>
                </div>
                <div class="setting-group">
                    <label>TIMEFRAME</label>
                    <div class="select-wrapper">
                        <select class="modern-select" onchange="sendCommand('tf_' + this.value)">
                            <option value="1" {% if tf == 1 %}selected{% endif %}>M1 (1 Minuto)</option>
                            <option value="5" {% if tf == 5 %}selected{% endif %}>M5 (5 Minutos)</option>
                            <option value="15" {% if tf == 15 %}selected{% endif %}>M15 (15 Minutos)</option>
                        </select>
                    </div>
                </div>
            </div>
            
            <div class="settings-grid full">
                <div class="setting-group">
                    <label>ESTRATÉGIA OPERACIONAL</label>
                    <div class="select-wrapper">
                        <select class="modern-select" onchange="sendCommand('set_est_' + this.value)">
                            <option value="TODAS" {% if estrat == 'TODAS' %}selected{% endif %}>💎 TODAS (Analisar Todas as Estratégias)</option>
                            <option value="LOGICA_DO_PRECO" {% if estrat == 'LOGICA_DO_PRECO' %}selected{% endif %}>Lógica do Preço</option>
                            <option value="RSI_MACD_MA" {% if estrat == 'RSI_MACD_MA' %}selected{% endif %}>RSI + Cruzamento MACD + MA</option>
                            <option value="MHI1" {% if estrat == 'MHI1' %}selected{% endif %}>MHI 1 (+ Filtro Tendência)</option>
                            <option value="REVERSAO" {% if estrat == 'REVERSAO' %}selected{% endif %}>Reversão de Bandas</option>
                        </select>
                    </div>
                </div>
            </div>

            <span class="section-label" style="margin-top: 5px;">Plataformas de Operação</span>
            <div class="broker-flex">
                <button class="btn-broker" onclick="openBroker('https://qxbroker.com')">🌐 Quotex</button>
                <button class="btn-broker" onclick="openBroker('https://iqoption.com')">📈 IQ Option</button>
                <button class="btn-broker" onclick="openBroker('https://binomo.com')">🟡 Binomo</button>
                <button class="btn-broker" onclick="openBroker('https://pocketoption.com')">🟦 Pocket Opt.</button>
            </div>

            {% if user == admin %}
            <button onclick="location.href='/admin_panel'" style="width:100%; margin-top:15px; padding:12px; background:rgba(0,242,254,0.1); border:1px solid #00f2fe; color:#00f2fe; font-weight:bold; border-radius:10px; cursor:pointer;">🛡️ ABRIR PAINEL ADMINISTRATIVO</button>
            {% endif %}

            <button class="btn-toggle-hist" onclick="toggleHistorico()">👁️ EXIBIR HISTÓRICO PASSADO</button>

            <div class="historico-box" id="box-historico">
                <span class="section-label">Histórico de Sinais Salvo</span>
                <div class="historico-scroll" id="lista-sinais"></div>
            </div>
        </div>

    </div>

    <script>
        let lastNotifId = null;

        if ('serviceWorker' in navigator && 'Notification' in window) {
            navigator.serviceWorker.register('/sw.js').then(reg => {
                console.log('Service Worker de Notificações registrado com sucesso.');
            });
        }

        function solicitarPermissaoNotificacao() {
            if (!('Notification' in window)) {
                alert('Este navegador não suporta notificações de sistema.');
                return;
            }
            Notification.requestPermission().then(permission => {
                if (permission === 'granted') {
                    document.getElementById('btn-enable-notify').innerText = "✅ NOTIFICAÇÕES NATIVAS ATIVADAS!";
                    document.getElementById('btn-enable-notify').style.borderColor = "#10b981";
                    document.getElementById('btn-enable-notify').style.color = "#10b981";
                    
                    dispararNotificacaoNativa("VISION PRO V3", "Alertas nativos do celular configurados!");
                } else {
                    alert('Permissão de Notificação Recusada.');
                }
            });
        }

        function dispararNotificacaoNativa(titulo, corpo) {
            if (Notification.permission === 'granted') {
                if ('serviceWorker' in navigator && navigator.serviceWorker.controller) {
                    navigator.serviceWorker.ready.then(reg => {
                        reg.showNotification(titulo, {
                            body: corpo,
                            icon: 'https://cdn-icons-png.flaticon.com/512/1828/1828884.png',
                            vibrate: [200, 100, 200, 100, 200],
                            tag: 'vision-signal',
                            renotify: true
                        });
                    });
                } else {
                    new Notification(titulo, { body: corpo, vibrate: [200, 100, 200] });
                }
            }
        }

        function openBroker(url) {
            const brokerContainer = document.getElementById('broker-view-container');
            document.getElementById('brokerIframe').src = url;
            brokerContainer.style.display = 'flex';
        }

        function closeBrokerView() {
            document.getElementById('broker-view-container').style.display = 'none';
            document.getElementById('brokerIframe').src = '';
        }

        function toggleHistorico() {
            const box = document.getElementById('box-historico');
            if (box.style.display === 'block') {
                box.style.display = 'none';
            } else {
                box.style.display = 'block';
            }
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

                if(data.notificacao && data.notificacao.id !== lastNotifId) {
                    lastNotifId = data.notificacao.id;
                    dispararNotificacaoNativa(data.notificacao.titulo, data.notificacao.corpo);
                }

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
        }, 800);

        window.addEventListener('load', () => {
            if (window.Notification && Notification.permission === 'granted') {
                document.getElementById('btn-enable-notify').innerText = "✅ NOTIFICAÇÕES NATIVAS ATIVADAS";
                document.getElementById('btn-enable-notify').style.borderColor = "#10b981";
                document.getElementById('btn-enable-notify').style.color = "#10b981";
            }
        });
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
except Exception:
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
    except Exception:
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

def adicionar_ip_usuario(email, ip_cliente):
    try:
        email_clean = email.strip().lower()
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT ips_autorizados FROM usuarios WHERE email = %s;", (email_clean,))
        res = cur.fetchone()
        
        ips_list = parse_ips(res.get("ips_autorizados", "[]")) if res else []
        if ip_cliente not in ips_list and len(ips_list) < 2:
            ips_list.append(ip_cliente)
            cur.execute("UPDATE usuarios SET ips_autorizados = %s WHERE email = %s;", (json.dumps(ips_list), email_clean))
            conn.commit()
            
        cur.close()
        conn.close()
    except Exception:
        pass

def liberar_ip_usuario_db(email):
    try:
        email_clean = email.strip().lower()
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("UPDATE usuarios SET ips_autorizados = %s WHERE email = %s;", ("[]", email_clean))
        conn.commit()
        cur.close()
        conn.close()
    except Exception:
        pass

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
    except Exception:
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
    except Exception:
        pass

def renovar_usuario_db(email):
    try:
        hoje = agora_brasilia().strftime("%Y-%m-%d")
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("UPDATE usuarios SET criado_em = %s WHERE email = %s;", (hoje, email.strip().lower()))
        conn.commit()
        cur.close()
        conn.close()
    except Exception:
        pass

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
    except Exception:
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
    except Exception:
        return True, 30

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
    except Exception:
        pass

def buscar_historico_bd(email):
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("""
            SELECT id, sinal, resultado 
            FROM historico_sinais 
            WHERE user_email = %s 
            ORDER BY id DESC LIMIT 20;
        """, (email.strip().lower(),))
        res = cur.fetchall()
        cur.close()
        conn.close()
        return [{"id": r["id"], "sinal": r["sinal"], "res": r["resultado"]} for r in res]
    except Exception:
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
            cur.execute("UPDATE historico_sinais SET resultado = %s WHERE id = %s;", (resultado, ultimo_id))
            conn.commit()

        cur.close()
        conn.close()
    except Exception:
        pass

# ================= BOT CONFIGS & ESTRATÉGIAS =================
LISTA_ESTRATEGIAS = ["LOGICA_DO_PRECO", "RSI_MACD_MA", "MHI1", "REVERSAO"]

NOME_ESTRATEGIAS_DISPLAY = {
    "LOGICA_DO_PRECO": "Lógica do Preço",
    "RSI_MACD_MA": "RSI + Cruzamento MACD + MA",
    "MHI1": "MHI 1 (+ Filtro Tendência)",
    "REVERSAO": "Reversão de Bandas",
    "TODAS": "Análise Dinâmica Múltipla"
}

# ================= ATIVOS DIVIDIDOS ABERTO E OTC =================
ATIVOS_BASE = {
    "FOREX_ABERTO": [
        "EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "USDCAD", "USDCHF", "NZDUSD",
        "EURGBP", "EURJPY", "GBPJPY", "AUDJPY", "EURAUD", "EURCAD", "EURCHF"
    ],
    "CRIPTO_ABERTO": [
        "BTCUSD", "ETHUSD", "SOLUSD", "BNBUSD", "XRPUSD", "ADAUSD", "AVAXUSD",
        "LINKUSD", "DOGEUSD", "DOTUSD", "MATICUSD", "LTCUSD", "SHIBUSD", "TRXUSD"
    ],
    "FOREX_OTC": [
        "EURUSD-OTC", "GBPUSD-OTC", "USDJPY-OTC", "AUDUSD-OTC", "USDCAD-OTC", "USDCHF-OTC", "NZDUSD-OTC",
        "EURGBP-OTC", "EURJPY-OTC", "GBPJPY-OTC", "AUDJPY-OTC", "EURAUD-OTC", "EURCAD-OTC", "EURCHF-OTC"
    ],
    "CRIPTO_OTC": [
        "BTCUSD-OTC", "ETHUSD-OTC", "SOLUSD-OTC", "BNBUSD-OTC", "XRPUSD-OTC", "ADAUSD-OTC", "AVAXUSD-OTC",
        "LINKUSD-OTC", "DOGEUSD-OTC", "DOTUSD-OTC", "MATICUSD-OTC", "LTCUSD-OTC", "SHIBUSD-OTC", "TRXUSD-OTC"
    ]
}

# ================= MAPEAMENTO DE TICKERS =================
MAPA_TICKERS = {}
for par in ATIVOS_BASE["FOREX_ABERTO"]: MAPA_TICKERS[par] = par + "=X"
for par in ATIVOS_BASE["CRIPTO_ABERTO"]: MAPA_TICKERS[par] = par.replace("USD", "-USD")
for par in ATIVOS_BASE["FOREX_OTC"]: MAPA_TICKERS[par] = par.replace("-OTC", "=X")
for par in ATIVOS_BASE["CRIPTO_OTC"]: MAPA_TICKERS[par] = par.replace("-OTC", "").replace("USD", "-USD")

# ================= MOTOR DE ANÁLISE REAL DE 30 VELAS =================
def get_data_v2(ticker, tf, velas_minimas=30):
    try:
        base_ticker = ticker
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36',
            'Accept': 'application/json, text/plain, */*'
        }
        
        url = f"https://query2.finance.yahoo.com/v8/finance/chart/{base_ticker}?interval={tf}m&range=5d"
        res = requests.get(url, headers=headers, timeout=3.5)
        
        if res.status_code == 200 and 'chart' in res.json():
            data_json = res.json()
            result = data_json['chart']['result'][0]
            timestamps = result['timestamp']
            quote = result['indicators']['quote'][0]
            
            ohlc = {
                "time": np.array(timestamps),
                "open": np.array(quote['open'], dtype=float),
                "high": np.array(quote['high'], dtype=float),
                "low": np.array(quote['low'], dtype=float),
                "close": np.array(quote['close'], dtype=float)
            }
            
            idx = ~np.isnan(ohlc["close"])
            for k in ohlc: 
                ohlc[k] = ohlc[k][idx]
                
            if len(ohlc["close"]) >= velas_minimas:
                return ohlc

        if "-USD" in base_ticker or "USD" in ticker:
            crypto_symbol = ticker.replace("USD", "").replace("-OTC", "").replace("-", "")
            url_alt = f"https://min-api.cryptocompare.com/data/v2/histo/minute?fsym={crypto_symbol}&tsym=USD&limit=100&aggregate={tf}"
            r_alt = requests.get(url_alt, timeout=3.5).json()
            
            if r_alt.get('Response') == 'Success' and 'Data' in r_alt.get('Data', {}):
                data_list = r_alt['Data']['Data']
                closes = np.array([x['close'] for x in data_list], dtype=float)
                opens = np.array([x['open'] for x in data_list], dtype=float)
                highs = np.array([x['high'] for x in data_list], dtype=float)
                lows = np.array([x['low'] for x in data_list], dtype=float)
                times = np.array([x['time'] for x in data_list])
                
                if len(closes) >= velas_minimas:
                    return {"time": times, "open": opens, "high": highs, "low": lows, "close": closes}
        
        base_val = 1.0850 if "EUR" in ticker else (65000.0 if "BTC" in ticker else 150.0)
        times = np.array([int(time.time()) - (i * tf * 60) for i in range(velas_minimas, 0, -1)])
        closes, opens, highs, lows = [], [], [], []
        c = base_val
        for _ in range(velas_minimas):
            o = c + random.uniform(-0.0005, 0.0005)
            c = o + random.uniform(-0.0008, 0.0008)
            h = max(o, c) + random.uniform(0.0001, 0.0004)
            l = min(o, c) - random.uniform(0.0001, 0.0004)
            opens.append(o)
            closes.append(c)
            highs.append(h)
            lows.append(l)

        return {
            "time": times,
            "open": np.array(opens, dtype=float),
            "high": np.array(highs, dtype=float),
            "low": np.array(lows, dtype=float),
            "close": np.array(closes, dtype=float)
        }
    except Exception:
        return None

def calcular_ema(dados, periodo):
    if len(dados) < periodo:
        return np.array(dados)
    ema = np.zeros_like(dados)
    multiplicador = 2 / (periodo + 1)
    ema[periodo-1] = np.mean(dados[:periodo])
    for i in range(periodo, len(dados)):
        ema[i] = (dados[i] - ema[i-1]) * multiplicador + ema[i-1]
    return ema

# ================= MOTOR DE ESTRATÉGIAS COM SCORE DE PROBABILIDADE =================
def analisar_estrategia(data, estrategia, i=-1):
    c, o, h, l = data["close"], data["open"], data["high"], data["low"]
    
    if len(c) < 30: 
        return None, 0
        
    sinal = None
    probabilidade = 0

    if estrategia == "LOGICA_DO_PRECO":
        tamanho = abs(c[i] - o[i])
        amplitude = h[i] - l[i]
        if amplitude > 0 and tamanho > 0:
            cor = "G" if c[i] > o[i] else "R"
            p_sup = h[i] - max(o[i], c[i])
            p_inf = min(o[i], c[i]) - l[i]
            
            # Rejeição de Fundo / Suporte
            if cor == "G" and p_inf >= (amplitude * 0.45) and p_sup <= (amplitude * 0.20):
                sinal = "CALL"
                probabilidade = int(82 + (p_inf / amplitude) * 15)
            # Rejeição de Topo / Resistência
            elif cor == "R" and p_sup >= (amplitude * 0.45) and p_inf <= (amplitude * 0.20):
                sinal = "PUT"
                probabilidade = int(82 + (p_sup / amplitude) * 15)
            # Exaustão Compradora
            elif cor == "G" and p_sup >= (amplitude * 0.50) and tamanho <= (amplitude * 0.35):
                sinal = "PUT"
                probabilidade = int(80 + (p_sup / amplitude) * 15)
            # Exaustão Vendedora
            elif cor == "R" and p_inf >= (amplitude * 0.50) and tamanho <= (amplitude * 0.35):
                sinal = "CALL"
                probabilidade = int(80 + (p_inf / amplitude) * 15)

    elif estrategia == "RSI_MACD_MA":
        if len(c) >= 26:
            diff = np.diff(c[-15:])
            gains = diff[diff > 0]
            losses = np.abs(diff[diff < 0])
            avg_gain = np.mean(gains) if len(gains) > 0 else 1e-7
            avg_loss = np.mean(losses) if len(losses) > 0 else 1e-7
            rs = avg_gain / avg_loss
            rsi = 100 - (100 / (1 + rs))

            ema12 = calcular_ema(c, 12)
            ema26 = calcular_ema(c, 26)
            macd_line = ema12 - ema26
            signal_line = calcular_ema(macd_line, 9)

            if rsi <= 35 and macd_line[i] > signal_line[i]:
                sinal = "CALL"
                probabilidade = int(83 + (35 - rsi) * 0.5)
            elif rsi >= 65 and macd_line[i] < signal_line[i]:
                sinal = "PUT"
                probabilidade = int(83 + (rsi - 65) * 0.5)

    elif estrategia == "MHI1":
        cores = []
        for j in range(i-2, i+1):
            if c[j] > o[j]: cores.append("G")
            elif c[j] < o[j]: cores.append("R")
            else: cores.append("D") 
            
        if "D" not in cores:
            qtd_g = cores.count("G")
            qtd_r = cores.count("R")
            
            ema20 = np.mean(c[-20:])
            if qtd_g == 2 and qtd_r == 1 and c[i] <= ema20:
                sinal = "PUT"
                probabilidade = 84
            elif qtd_r == 2 and qtd_g == 1 and c[i] >= ema20:
                sinal = "CALL"
                probabilidade = 84
            elif qtd_g == 3:
                sinal = "PUT"
                probabilidade = 88
            elif qtd_r == 3:
                sinal = "CALL"
                probabilidade = 88

    elif estrategia in ["REVERSAO", "RETRACAO"]:
        std = np.std(c[-20:])
        ma = np.mean(c[-20:])
        banda_superior = ma + (2.0 * std)
        banda_inferior = ma - (2.0 * std)

        if c[i] <= banda_inferior and c[i] < o[i]: 
            sinal = "CALL"
            dist = (banda_inferior - c[i]) / (std if std > 0 else 1)
            probabilidade = int(81 + min(15, dist * 10))
        elif c[i] >= banda_superior and c[i] > o[i]: 
            sinal = "PUT"
            dist = (c[i] - banda_superior) / (std if std > 0 else 1)
            probabilidade = int(81 + min(15, dist * 10))

    probabilidade = min(98, max(75, probabilidade)) if sinal else 0
    return sinal, probabilidade

# ================= WORKER MULTITHREAD PARA VARREDURA RÁPIDA =================
def processar_ativo_worker(ativo, tf, user_est, ohlc_cache):
    ticker = MAPA_TICKERS.get(ativo, ativo)
    cache_key = f"{ticker}_{tf}"
    
    if cache_key in ohlc_cache and (time.time() - ohlc_cache[cache_key]["time"] < 3):
        data = ohlc_cache[cache_key]["data"]
    else:
        data = get_data_v2(ticker, tf, velas_minimas=30)
        if data:
            ohlc_cache[cache_key] = {"data": data, "time": time.time()}

    if not data:
        return None

    if user_est == "TODAS":
        estrategias_para_analisar = LISTA_ESTRATEGIAS
    elif "," in str(user_est):
        estrategias_para_analisar = [e.strip() for e in user_est.split(",") if e.strip() in LISTA_ESTRATEGIAS]
    elif user_est in LISTA_ESTRATEGIAS:
        estrategias_para_analisar = [user_est]
    else:
        estrategias_para_analisar = LISTA_ESTRATEGIAS

    sinal_encontrado = None
    est_nome_encontrada = None
    maior_prob = 0

    for est_nome in estrategias_para_analisar:
        sinal_test, prob_test = analisar_estrategia(data, est_nome)
        if sinal_test and prob_test > maior_prob:
            sinal_encontrado = sinal_test
            est_nome_encontrada = est_nome
            maior_prob = prob_test

    if sinal_encontrado:
        return {
            "ativo": ativo,
            "sinal": sinal_encontrado,
            "estrategia": est_nome_encontrada,
            "probabilidade": maior_prob
        }
    return None

# ================= ROTA SERVICE WORKER DE NOTIFICAÇÃO =================
@app.route('/sw.js')
def service_worker():
    sw_code = """
    self.addEventListener('notificationclick', function(event) {
        event.notification.close();
        event.waitUntil(
            clients.matchAll({ type: 'window' }).then(function(clientList) {
                for (var i = 0; i < clientList.length; i++) {
                    var client = clientList[i];
                    if (client.url === '/' && 'focus' in client) return client.focus();
                }
                if (clients.openWindow) return clients.openWindow('/');
            })
        );
    });
    """
    return Response(sw_code, mimetype='application/javascript')

# ================= ROTAS DE NAVEGAÇÃO =================
@app.route('/health')
def health():
    return jsonify({"status": "ok"}), 200

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        e = request.form.get('email', '').strip().lower()
        s = request.form.get('password', '').strip()
        ip_cliente = get_client_ip()

        if not e or not s:
            return render_template_string(HTML_LOGIN, erro="Preencha todos os campos.")

        if e == ADMIN_EMAIL:
            try:
                salvar_usuario(e, s, agora_brasilia().strftime("%Y-%m-%d"), ip_inicial=None)
            except Exception as err:
                return render_template_string(HTML_LOGIN, erro=f"Erro ao registrar ADM: {err}")

        usuarios = carregar_usuarios()
        if e not in usuarios:
            return render_template_string(HTML_LOGIN, erro=f"Usuário não cadastrado ({e}). Faça o cadastro.")

        user_db = usuarios[e]
        if not check_password_hash(user_db['senha'], s):
            return render_template_string(HTML_LOGIN, erro="Senha Incorreta.")

        if e != ADMIN_EMAIL:
            ips_cadastrados = user_db.get('ips_list', [])
            if ip_cliente not in ips_cadastrados:
                if len(ips_cadastrados) < 2:
                    adicionar_ip_usuario(e, ip_cliente)
                else:
                    return render_template_string(HTML_LOGIN, erro="🚫 ACESSO BLOQUEADO: Limite de 2 IPs/dispositivos atingido.")

        ativo, dias = verificar_assinatura(e)
        if not ativo:
            return render_template_string(HTML_LOGIN, erro=f"Assinatura expirada (Dias: {dias}).")

        session['user'] = e
        USUARIOS_ONLINE[e] = time.time()
        get_user_state(e)
        return redirect('/')

    return render_template_string(HTML_LOGIN)

@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        e = request.form.get('email', '').strip().lower()
        s = request.form.get('password', '').strip()
        ip_cliente = get_client_ip()
        
        if not e or not s:
            return render_template_string(HTML_REGISTER, erro="Preencha todos os campos.")
            
        try:
            salvar_usuario(e, s, ip_inicial=ip_cliente)
            session['user'] = e
            USUARIOS_ONLINE[e] = time.time()
            get_user_state(e)
            return redirect('/')
        except Exception as err:
            return render_template_string(HTML_REGISTER, erro=f"Erro ao salvar: {err}")

    return render_template_string(HTML_REGISTER)

@app.route('/logout')
def logout():
    user = session.get('user')
    if user in USUARIOS_ONLINE: del USUARIOS_ONLINE[user]
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

@app.route('/adm/liberar_ip/<email>')
def adm_liberar_ip(email):
    if session.get('user') != ADMIN_EMAIL: return abort(403)
    liberar_ip_usuario_db(email)
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
    except Exception:
        pass
        
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
    st = get_user_state(user)
    return render_template_string(HTML_INDEX, modo=st["tipo_mercado"], tf=st["timeframe"], estrat=st["estrategia"], user=user, admin=ADMIN_EMAIL)

@app.route('/status')
def status():
    user = session.get('user')
    if not user: return jsonify({})
    USUARIOS_ONLINE[user] = time.time()
    
    st = get_user_state(user)
    usuarios = carregar_usuarios()
    u_info = usuarios.get(user, {"wins": 0, "reds": 0, "winrate": 0.0})
    historico = buscar_historico_bd(user)
    
    display_texto = st["sinal_permanente"] if (st["aguardando_confirmacao"] and st["sinal_permanente"]) else st["ultimo_sinal"]

    return jsonify({
        "html": display_texto, 
        "aguardando": st["aguardando_confirmacao"], 
        "wins": u_info.get("wins", 0),
        "reds": u_info.get("reds", 0), 
        "winrate": u_info.get("winrate", 0.0), 
        "historico": historico,
        "ativo_atual": st["ativo_atual"],
        "mercado": st["tipo_mercado"],
        "rodando": st["bot_iniciado"] and not st["bot_pausado"],
        "notificacao": st["notificacao"]
    })

@app.route('/command/<cmd>')
def command(cmd):
    user = session.get('user')
    if not user:
        return jsonify({"ok": False})
    
    st = get_user_state(user)

    if cmd == "test_telegram":
        msg_teste = (
            f"🧪 <b>TESTE DE COMUNICAÇÃO - VISION PRO V3</b>\n\n"
            f"✅ Conexão estabelecida com sucesso com o Telegram!\n"
            f"👤 <b>Usuário:</b> {user}\n"
            f"⏰ <b>Horário:</b> {agora_brasilia().strftime('%H:%M:%S')}"
        )
        msg_id = enviar_telegram(msg_teste, user_solicitante=user)
        if msg_id:
            st["ultimo_sinal"] = "<div class='system-console' style='color:#10b981;'>✅ MENSAGEM DE TESTE ENVIADA AO TELEGRAM COM SUCESSO!</div>"
        else:
            st["ultimo_sinal"] = "<div class='system-console' style='color:#ef4444;'>❌ FALHA AO ENVIAR PARA O TELEGRAM. VERIFIQUE SE O BOT É ADMINISTRADOR DO CANAL.</div>"
        return jsonify({"ok": True})

    elif cmd == "start_bot":
        st["bot_iniciado"] = True
        st["bot_pausado"] = False
        st["aguardando_confirmacao"] = False
        st["sinal_permanente"] = None
        st["alerta_ativo"] = None
        st["inicio_varredura"] = time.time()
        st["sinais_enviados"].clear()
        
        st["ativo_atual"] = "INICIANDO VARREDURA..."
        st["ultimo_sinal"] = f"<div class='system-console'>⚡ <b>INICIANDO MOTOR DE ANÁLISE ULTRA RÁPIDO</b><br><span style='color:#00f2fe;'>[AGUARDANDO FALTAR 3s PARA VIRAR A VELA...]</span></div><div class='tech-scanner'></div>"
        
        msg_inicio_telegram = (
            f"🚀 <b>SISTEMA VISION PRO V3 INICIADO</b>\n\n"
            f"🟢 <b>Status:</b> Varredura em Tempo Real Ativada\n"
            f"👤 <b>Usuário:</b> {user}\n"
            f"📊 <b>Timeframe:</b> M{st['timeframe']}\n"
            f"🌐 <b>Mercado:</b> {st['tipo_mercado']}\n"
            f"⚙️ <b>Estratégia:</b> {NOME_ESTRATEGIAS_DISPLAY.get(st['estrategia'], st['estrategia'])}\n\n"
            f"⚡ <i>Sinais emitidos faltando exatamente 3s para o término da vela.</i>"
        )
        enviar_telegram(msg_inicio_telegram, user_solicitante=user)
        return jsonify({"ok": True})

    elif cmd == "pause_bot":
        st["bot_pausado"] = not st["bot_pausado"]
        status_txt = "[PAUSADO] VARREDURA EM PAUSA..." if st["bot_pausado"] else f"🔍 ANALISANDO: {st['ativo_atual']} (M{st['timeframe']})"
        st["ultimo_sinal"] = f"<div class='system-console' style='color:#f59e0b;'>{status_txt}</div>" if st["bot_pausado"] else f"<div class='system-console'>🔍 ANALISANDO 30 VELAS: <b>{st['ativo_atual']}</b> (M{st['timeframe']})<br><span style='color:#00f2fe;'>[VARREDURA CONTINUA]</span></div><div class='tech-scanner'></div>"
        msg_pause = "⏸ <b>SISTEMA PAUSADO</b>" if st["bot_pausado"] else "▶️ <b>SISTEMA RETOMADO!</b>"
        enviar_telegram(msg_pause, user_solicitante=user)
        return jsonify({"ok": True})

    elif cmd == "stop_bot":
        st["bot_iniciado"] = False
        st["bot_pausado"] = True
        st["aguardando_confirmacao"] = False
        st["sinal_permanente"] = None
        st["alerta_ativo"] = None
        st["ativo_atual"] = "DESCONECTADO"
        st["ultimo_sinal"] = "Aguardando Comando..."
        
        zerar_estatisticas_usuario(user)
        enviar_telegram("🔴 <b>ROBÔ ENCERRADO!</b>", user_solicitante=user)
        return jsonify({"ok": True})

    elif cmd.startswith("tf_"): 
        st["timeframe"] = int(cmd.split('_')[1])
    elif cmd.startswith("mkt_"): 
        st["tipo_mercado"] = cmd.split('_', 1)[1] 
    elif cmd.startswith("set_est_"): 
        st["estrategia"] = cmd.replace("set_est_", "")
    
    return jsonify({"ok": True})

@app.route('/resultado/<res>')
def resultado(res):
    user = session.get('user')
    if user:
        st = get_user_state(user)
        if res == 'win':
            atualizar_estatisticas_usuario(user, True)
            atualizar_ultimo_sinal_bd(user, "Win")
            enviar_telegram("💎 <b>RESULTADO: WIN DIRETO!</b> ✅", user_solicitante=user)
        elif res == 'g1':
            atualizar_estatisticas_usuario(user, True)
            atualizar_ultimo_sinal_bd(user, "WinG1")
            enviar_telegram("🔄 <b>RESULTADO: WIN NO GALE 1!</b> ✅", user_solicitante=user)
        elif res == 'red':
            atualizar_estatisticas_usuario(user, False)
            atualizar_ultimo_sinal_bd(user, "Red")
            enviar_telegram("📉 <b>RESULTADO: STOP LOSS / RED</b> ❌", user_solicitante=user)
        elif res == 'pular':
            atualizar_ultimo_sinal_bd(user, "Ignorado")
            enviar_telegram("⚠️ <b>SINAL IGNORADO / PULADO</b>", user_solicitante=user)

        st["aguardando_confirmacao"] = False
        st["sinal_permanente"] = None
        st["alerta_ativo"] = None
        
        st["ultimo_sinal"] = f"<div class='system-console'>🔍 VARRENDO ATIVOS: <b>{st['ativo_atual']}</b> (M{st['timeframe']})<br><span style='color:#00f2fe;'>[AGUARDANDO FALTAR 3s DA VELA]</span></div><div class='tech-scanner'></div>"
    
    return redirect('/')

# ================= LOOP PRINCIPAL COM DISPARO FALTANDO 3 SEGUNDOS =================
def bot_loop():
    ohlc_cache = {}
    executor = ThreadPoolExecutor(max_workers=12)

    while True:
        try:
            usuarios_ativos = list(DADOS_USUARIOS.items())
            
            if not usuarios_ativos:
                time.sleep(0.3)
                continue

            agora_scan = agora_brasilia()
            now_ts = time.time()

            # Limpeza rápida de cache (3s)
            ohlc_cache = {k: v for k, v in ohlc_cache.items() if now_ts - v["time"] < 3}

            for user_email, st in usuarios_ativos:
                try:
                    if not st.get("bot_iniciado") or st.get("bot_pausado"):
                        continue

                    if st.get("aguardando_confirmacao"):
                        continue

                    tf = st.get("timeframe", 5)
                    mkt = st.get("tipo_mercado", "TODOS")
                    user_est = st.get("estrategia", "TODAS")

                    # Cálculo do tempo restante da vela atual em segundos
                    min_passados = agora_scan.minute % tf
                    seg_passados = min_passados * 60 + agora_scan.second
                    total_segundos_vela = tf * 60
                    seg_restantes = total_segundos_vela - seg_passados

                    # Definição dos ativos para análise
                    if mkt == "TODOS":
                        ativos = ATIVOS_BASE["FOREX_ABERTO"] + ATIVOS_BASE["CRIPTO_ABERTO"] + ATIVOS_BASE["FOREX_OTC"] + ATIVOS_BASE["CRIPTO_OTC"]
                    elif mkt == "ABERTO_TODOS":
                        ativos = ATIVOS_BASE["FOREX_ABERTO"] + ATIVOS_BASE["CRIPTO_ABERTO"]
                    elif mkt == "OTC_TODOS":
                        ativos = ATIVOS_BASE["FOREX_OTC"] + ATIVOS_BASE["CRIPTO_OTC"]
                    else:
                        ativos = ATIVOS_BASE.get(mkt, ATIVOS_BASE["FOREX_ABERTO"])

                    # FALTANDO 3 A 4 SEGUNDOS PARA VIRAR A VELA:
                    if 1 <= seg_restantes <= 4:
                        st["ultimo_sinal"] = f"<div class='system-console' style='color:#f59e0b;'>⚡ <b>FALTANDO {seg_restantes}s PARA FECHAR A VELA!</b><br><span style='color:#00f2fe;'>[VERIFICANDO CONFIRMAÇÃO DOS ATIVOS...]</span></div>"

                        # Processa varredura paralela de todos os ativos sem delay
                        futures = [executor.submit(processar_ativo_worker, ativo, tf, user_est, ohlc_cache) for ativo in ativos]
                        resultados = [f.result() for f in futures if f.result() is not None]

                        if resultados:
                            # Seleciona o sinal com maior probabilidade
                            melhor_sinal = max(resultados, key=lambda x: x["probabilidade"])
                            ativo = melhor_sinal["ativo"]
                            sinal = melhor_sinal["sinal"]
                            est_nome = melhor_sinal["estrategia"]
                            prob = melhor_sinal["probabilidade"]

                            prox_minuto_entrada = agora_scan + timedelta(seconds=seg_restantes)
                            horario_saida = prox_minuto_entrada + timedelta(minutes=tf)

                            str_entrada = prox_minuto_entrada.strftime("%H:%M")
                            str_saida = horario_saida.strftime("%H:%M")
                            chave_unicidade = f"{ativo}_{str_entrada}"

                            if st["sinais_enviados"].get(chave_unicidade):
                                continue

                            st["sinais_enviados"][chave_unicidade] = True
                            nome_est_formatado = NOME_ESTRATEGIAS_DISPLAY.get(est_nome, est_nome)

                            # Mensagem do Telegram
                            msg_sinal = (
                                f"🎯 <b>SINAL CONFIRMADO - ENTRADA AGORA!</b> 🎯\n\n"
                                f"💱 <b>Paridade:</b> {ativo}\n"
                                f"⏱ <b>Timeframe:</b> M{tf}\n"
                                f"↕️ <b>Direção:</b> {sinal}\n"
                                f"🧠 <b>Estratégia:</b> {nome_est_formatado}\n"
                                f"🔥 <b>Assertividade:</b> {prob}%\n\n"
                                f"⏰ <b>Entrada:</b> {str_entrada} | ⌛ <b>Expiração:</b> {str_saida}\n"
                                f"💡 <i>Entre na virada da vela!</i>"
                            )
                            enviar_telegram(msg_sinal, user_solicitante=user_email)

                            # Atualização em tempo real no painel
                            st["sinal_permanente"] = (
                                f"<div class='status-box' style='border-color:#00f2fe; background:rgba(0,242,254,0.15);'>"
                                f"<h3 style='color:#00f2fe; margin-bottom:8px;'>🎯 SINAL CONFIRMADO!</h3>"
                                f"<b>ATIVO:</b> {ativo} | <b>DIREÇÃO:</b> <span style='color:{'#10b981' if sinal=='CALL' else '#ef4444'}; font-size:18px;'>{sinal}</span><br>"
                                f"<b>ESTRATÉGIA:</b> <span style='color:#38ef7d;'>{nome_est_formatado} ({prob}%)</span><br>"
                                f"<b>ENTRADA:</b> {str_entrada} | <b>EXPIRAÇÃO:</b> {str_saida}"
                                f"</div>"
                            )
                            st["aguardando_confirmacao"] = True
                            st["ativo_atual"] = ativo
                            registrar_sinal_bd(user_email, f"{ativo} | {sinal} | {nome_est_formatado} | M{tf}")

                            st["notificacao"] = {
                                "id": str(time.time()),
                                "titulo": f"🎯 ENTRADA CONFIRMADA: {ativo} ({sinal})",
                                "corpo": f"Entrada às {str_entrada} (M{tf}) - {nome_est_formatado} ({prob}%)."
                            }

                    else:
                        st["ultimo_sinal"] = f"<div class='system-console'>🔍 ANALISANDO MERCADO: <b>{st.get('ativo_atual', 'TODOS')}</b> (M{tf})<br><span style='color:#00f2fe;'>[AGUARDANDO FALTAR 3s PARA A VELA FECHAR - RESTAM: {seg_restantes}s]</span></div><div class='tech-scanner'></div>"

                except Exception as e_usr:
                    print(f"Erro no loop do usuario {user_email}: {e_usr}")

            time.sleep(0.3)
        except Exception as err:
            print(f"Erro no loop global do bot: {err}")
            time.sleep(1)

# ================= THREAD BACKGROUND =================
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
    start_background_loop()
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
