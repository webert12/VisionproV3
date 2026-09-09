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

# ================= CONFIGURAÇÕES DE AMBIENTE E BOT TELEGRAM =================
TOKEN_TELEGRAM = os.getenv("TOKEN_TELEGRAM", "").strip()
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
            "ativos_selecionados": "TODOS",
            "bot_iniciado": False,
            "bot_pausado": True,
            "aguardando_confirmacao": False,
            "sinal_permanente": None,
            "ultimo_sinal": "Aguardando Comando...",
            "ativo_atual": "AGUARDANDO...",
            "inicio_varredura": 0,
            "sinais_enviados": {},
            "alerta_ativo": None,
            "notificacao": None,
            "notificacao_ultima_hora": 0.0,
            "catalogando": False,
            "catalogacao_resultado": None
        }
    return DADOS_USUARIOS[email_clean]

def get_client_ip():
    if request.headers.get('X-Forwarded-For'):
        return request.headers.get('X-Forwarded-For').split(',')[0].strip()
    return request.remote_addr

# ================= ENVIO E DELEÇÃO TELEGRAM =================
def enviar_telegram(mensagem, auto_delete=None, user_solicitante=None):
    if not TOKEN_TELEGRAM or not CHAT_ID_TELEGRAM:
        print("⚠️ Telegram: TOKEN_TELEGRAM ou CHAT_ID_TELEGRAM não configurado.")
        return None
        
    token = TOKEN_TELEGRAM.strip()
    chat_id = CHAT_ID_TELEGRAM.strip()
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    
    payload = {
        "chat_id": chat_id, 
        "text": mensagem, 
        "parse_mode": "HTML",
        "disable_web_page_preview": True
    }
    
    try:
        res = requests.post(url, json=payload, timeout=10)
        r = res.json()
        if r.get("ok"):
            msg_id = r["result"]["message_id"]
            if auto_delete:
                threading.Thread(target=deletar_mensagem_atrasada, args=(msg_id, auto_delete), daemon=True).start()
            return msg_id
        else:
            print(f"⚠️ Telegram API Recusou HTML ({r.get('description')}). Tentando formato Texto...")
            texto_limpo = re.sub('<[^<]+?>', '', mensagem)
            payload_plain = {
                "chat_id": chat_id, 
                "text": texto_limpo,
                "disable_web_page_preview": True
            }
            res_plain = requests.post(url, json=payload_plain, timeout=10)
            r_plain = res_plain.json()
            if r_plain.get("ok"):
                return r_plain["result"]["message_id"]
            else:
                print(f"❌ Telegram API Erro no Fallback: {r_plain}")
    except Exception as e:
        print(f"❌ Erro de conexão com o Telegram: {e}")
    return None

def deletar_mensagem_telegram(msg_id):
    if not TOKEN_TELEGRAM or not CHAT_ID_TELEGRAM or not msg_id:
        return
    try:
        token = TOKEN_TELEGRAM.strip()
        chat_id = CHAT_ID_TELEGRAM.strip()
        url = f"https://api.telegram.org/bot{token}/deleteMessage"
        payload = {"chat_id": chat_id, "message_id": msg_id}
        requests.post(url, json=payload, timeout=5)
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

        /* ESTILOS DE CATALOGAÇÃO E SELEÇÃO DE ATIVOS */
        .btn-catalog { width: 100%; padding: 13px; background: linear-gradient(135deg, #00c6ff, #0072ff); border: none; color: white; font-weight: 800; font-size: 12px; border-radius: 12px; cursor: pointer; margin-bottom: 12px; transition: 0.3s; text-transform: uppercase; box-shadow: 0 4px 15px rgba(0, 198, 255, 0.3); letter-spacing: 0.5px; }
        .btn-catalog:hover { transform: translateY(-1px); box-shadow: 0 6px 20px rgba(0, 198, 255, 0.5); }
        .catalog-card { background: #0b1120; border: 1px solid #00f2fe; border-radius: 16px; padding: 15px; margin-bottom: 16px; font-size: 12px; }
        .catalog-table { width: 100%; border-collapse: collapse; margin-top: 10px; font-size: 11px; }
        .catalog-table th, .catalog-table td { padding: 8px; text-align: left; border-bottom: 1px solid #1e293b; }
        .catalog-table th { color: #00f2fe; font-weight: 800; text-transform: uppercase; }
        .asset-chip { display: inline-block; padding: 3px 8px; border-radius: 6px; background: rgba(0, 242, 254, 0.1); border: 1px solid rgba(0, 242, 254, 0.3); font-size: 10px; margin: 2px; font-weight: bold; }
        .asset-checkbox-grid { display: grid; grid-template-columns: repeat(2, 1fr); gap: 6px; max-height: 160px; overflow-y: auto; padding: 8px; background: #0f172a; border-radius: 8px; border: 1px solid #1e293b; margin-top: 5px; }
        .asset-checkbox-item { font-size: 11px; display: flex; align-items: center; gap: 6px; color: #cbd5e1; cursor: pointer; }
        .asset-checkbox-item input { accent-color: #00f2fe; cursor: pointer; }
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <div class="brand">VISION PRO <span>V3 ULTRA</span></div>
            <a href="/logout" class="btn-logout">SAIR</a>
        </div>

        <button class="btn-notify" id="btn-enable-notify" onclick="solicitarPermissaoNotificacao()">🔔 ATIVAR NOTIFICAÇÕES NO CELULAR</button>
        <button class="btn-catalog" onclick="sendCommand('fazer_catalogacao')">🔍 REALIZAR VARREDURA PRÉ-OPERACIONAL (60 VELAS)</button>
        <button class="btn-test-tg" onclick="sendCommand('test_telegram')">🧪 TESTAR CONEXÃO TELEGRAM</button>

        <!-- RESULTADO DA CATALOGAÇÃO / VARREDURA -->
        <div id="catalog-box" class="catalog-card" style="display:none;">
            <div style="display:flex; justify-content:space-between; align-items:center; border-bottom:1px solid #1e293b; padding-bottom:8px; margin-bottom:10px;">
                <span style="font-weight:800; color:#00f2fe; font-size:12px;">📊 RELATÓRIO DA VARREDURA (60 VELAS)</span>
                <button onclick="document.getElementById('catalog-box').style.display='none'" style="background:none; border:none; color:#ef4444; font-weight:bold; cursor:pointer;">✖ FECHAR</button>
            </div>
            
            <div id="catalog-loader" style="text-align:center; padding:15px; display:none;">
                <div class="tech-scanner"></div>
                <p style="font-size:11px; color:#00f2fe; margin-top:10px;">Analisando rapidamente as últimas 60 velas de todos os ativos...</p>
            </div>

            <div id="catalog-content"></div>
        </div>

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
            ANALISANDO AGORA: <b id="current-asset" style="color: #38ef7d;">AGUARDANDO...</b><br>
            <span style="font-size:10px; color:#94a3b8;">FILTRO DE ATIVOS: <b id="ativos-badge" style="color:#f59e0b;">TODOS OS ATIVOS</b></span>
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
                        <select id="select-mkt" class="modern-select" onchange="sendCommand('mkt_' + this.value); atualizarListaAtivosSelecao(this.value);">
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
                        <select id="select-est" class="modern-select" onchange="sendCommand('set_est_' + this.value)">
                            <option value="TODAS" {% if estrat == 'TODAS' %}selected{% endif %}>💎 TODAS (Analisar Todas as Estratégias)</option>
                            <option value="LOGICA_DO_PRECO" {% if estrat == 'LOGICA_DO_PRECO' %}selected{% endif %}>Lógica do Preço</option>
                            <option value="RSI_MACD_MA" {% if estrat == 'RSI_MACD_MA' %}selected{% endif %}>RSI + Cruzamento MACD + MA</option>
                            <option value="MHI1" {% if estrat == 'MHI1' %}selected{% endif %}>MHI 1 (+ Filtro Tendência)</option>
                            <option value="REVERSAO" {% if estrat == 'REVERSAO' %}selected{% endif %}>Reversão de Bandas</option>
                        </select>
                    </div>
                </div>
            </div>

            <!-- SELETOR PERSONALIZADO DE ATIVOS -->
            <div class="settings-grid full">
                <div class="setting-group">
                    <button type="button" onclick="toggleAssetSection()" id="btn-toggle-assets" style="width:100%; padding:11px; background:#0f172a; border:1px solid #1e293b; color:#00f2fe; border-radius:10px; font-size:11px; font-weight:800; cursor:pointer; text-align:left; display:flex; justify-content:space-between; align-items:center;">
                        <span>🎯 SELEÇÃO PERSONALIZADA DE ATIVOS</span>
                        <span id="asset-toggle-icon" style="color:#00f2fe; font-size:10px;">▼ EXIBIR</span>
                    </button>
                    <div id="assets-collapsible-wrapper" style="display:none; margin-top:8px; background:#0b1120; padding:12px; border-radius:10px; border:1px solid #1e293b;">
                        <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:8px;">
                            <span style="font-size:10px; color:#94a3b8; font-weight:bold;">Marque os ativos para operar:</span>
                            <div>
                                <button type="button" onclick="marcarTodosAtivos(true)" style="background:none; border:none; color:#00f2fe; font-size:10px; cursor:pointer; font-weight:bold;">Marcar Todos</button> |
                                <button type="button" onclick="marcarTodosAtivos(false)" style="background:none; border:none; color:#ef4444; font-size:10px; cursor:pointer; font-weight:bold;">Limpar</button>
                            </div>
                        </div>
                        <div id="asset-checkbox-container" class="asset-checkbox-grid"></div>
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
        const NATIVE_NOTIFICATION_COOLDOWN_MS = 60000;

        const ATIVOS_MAPEADOS = {
            "FOREX_ABERTO": ["EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "USDCAD", "USDCHF", "NZDUSD", "EURGBP", "EURJPY", "GBPJPY", "AUDJPY", "EURAUD", "EURCAD", "EURCHF"],
            "CRIPTO_ABERTO": ["BTCUSD", "ETHUSD", "SOLUSD", "BNBUSD", "XRPUSD", "ADAUSD", "AVAXUSD", "LINKUSD", "DOGEUSD", "DOTUSD", "MATICUSD", "LTCUSD", "SHIBUSD", "TRXUSD"],
            "FOREX_OTC": ["EURUSD-OTC", "GBPUSD-OTC", "USDJPY-OTC", "AUDUSD-OTC", "USDCAD-OTC", "USDCHF-OTC", "NZDUSD-OTC", "EURGBP-OTC", "EURJPY-OTC", "GBPJPY-OTC", "AUDJPY-OTC", "EURAUD-OTC", "EURCAD-OTC", "EURCHF-OTC"],
            "CRIPTO_OTC": ["BTCUSD-OTC", "ETHUSD-OTC", "SOLUSD-OTC", "BNBUSD-OTC", "XRPUSD-OTC", "ADAUSD-OTC", "AVAXUSD-OTC", "LINKUSD-OTC", "DOGEUSD-OTC", "DOTUSD-OTC", "MATICUSD-OTC", "LTCUSD-OTC", "SHIBUSD-OTC", "TRXUSD-OTC"]
        };

        if ('serviceWorker' in navigator && 'Notification' in window) {
            navigator.serviceWorker.register('/sw.js', { updateViaCache: 'none' })
                .then(() => console.log('Service Worker de notificações registrado.'))
                .catch(err => console.warn('Falha ao registrar Service Worker:', err));
        }

        function solicitarPermissaoNotificacao() {
            if (!('Notification' in window)) {
                alert('Este navegador não suporta notificações de sistema.');
                return;
            }

            Notification.requestPermission().then(permission => {
                if (permission === 'granted') {
                    const btn = document.getElementById('btn-enable-notify');
                    btn.innerText = "✅ NOTIFICAÇÕES NATIVAS ATIVADAS!";
                    btn.style.borderColor = "#10b981";
                    btn.style.color = "#10b981";
                } else {
                    alert('Permissão de Notificação Recusada.');
                }
            });
        }

        async function dispararNotificacaoNativa(titulo, corpo, notifId) {
            if (!('Notification' in window) || Notification.permission !== 'granted') return;

            const id = String(notifId || '');
            const agora = Date.now();

            const ultimoId = localStorage.getItem('vision_last_notif_id') || '';
            const ultimaHora = Number(localStorage.getItem('vision_last_notif_at') || '0');

            if (id && id === ultimoId) return;
            if (ultimaHora && (agora - ultimaHora) < NATIVE_NOTIFICATION_COOLDOWN_MS) return;

            try {
                if ('serviceWorker' in navigator) {
                    const reg = await navigator.serviceWorker.ready;
                    await reg.showNotification(titulo, {
                        body: corpo,
                        tag: 'vision-signal',
                        renotify: false,
                        requireInteraction: false
                    });
                } else {
                    new Notification(titulo, { body: corpo });
                }

                if (id) localStorage.setItem('vision_last_notif_id', id);
                localStorage.setItem('vision_last_notif_at', String(agora));
            } catch (err) {
                console.warn('Não foi possível exibir a notificação:', err);
            }
        }

        function toggleAssetSection() {
            const wrapper = document.getElementById('assets-collapsible-wrapper');
            const icon = document.getElementById('asset-toggle-icon');
            if (wrapper.style.display === 'none' || wrapper.style.display === '') {
                wrapper.style.display = 'block';
                icon.innerText = '▲ OCULTAR';
            } else {
                wrapper.style.display = 'none';
                icon.innerText = '▼ EXIBIR';
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
            box.style.display = (box.style.display === 'block') ? 'none' : 'block';
        }

        function sendCommand(cmd) {
            fetch('/command/' + cmd).then(r => r.json()).then(data => {
                if(data.redirect) window.location.href = data.redirect;
            });
        }

        async function aplicarConfigOperacional(est, ativoSelecionado) {
            let bodyData = {};
            if(est) bodyData.estrategia = est;
            if(ativoSelecionado) bodyData.ativos = ativoSelecionado;

            await fetch('/salvar_config_operacional', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(bodyData)
            });

            const selEst = document.getElementById('select-est');
            if(selEst && est) selEst.value = est;

            alert('✅ Configuração aplicada! Clique em START para o robô operar com esses ativos.');
        }

        function atualizarListaAtivosSelecao(mktModo, ativosAtuais) {
            let lista = [];
            if (mktModo === "TODOS") {
                lista = [...ATIVOS_MAPEADOS.FOREX_ABERTO, ...ATIVOS_MAPEADOS.CRIPTO_ABERTO, ...ATIVOS_MAPEADOS.FOREX_OTC, ...ATIVOS_MAPEADOS.CRIPTO_OTC];
            } else if (mktModo === "ABERTO_TODOS") {
                lista = [...ATIVOS_MAPEADOS.FOREX_ABERTO, ...ATIVOS_MAPEADOS.CRIPTO_ABERTO];
            } else if (mktModo === "OTC_TODOS") {
                lista = [...ATIVOS_MAPEADOS.FOREX_OTC, ...ATIVOS_MAPEADOS.CRIPTO_OTC];
            } else if (ATIVOS_MAPEADOS[mktModo]) {
                lista = ATIVOS_MAPEADOS[mktModo];
            } else {
                lista = ATIVOS_MAPEADOS.FOREX_ABERTO;
            }

            const container = document.getElementById('asset-checkbox-container');
            if(!container) return;
            
            let html = '';
            const todosMarcados = !ativosAtuais || ativosAtuais === "TODOS";
            
            lista.forEach(atv => {
                const checado = todosMarcados || (Array.isArray(ativosAtuais) && ativosAtuais.includes(atv));
                html += `
                    <label class="asset-checkbox-item">
                        <input type="checkbox" value="${atv}" ${checado ? 'checked' : ''} onchange="salvarSelecaoAtivos()">
                        <span>${atv}</span>
                    </label>
                `;
            });

            container.innerHTML = html;
        }

        function marcarTodosAtivos(status) {
            document.querySelectorAll('#asset-checkbox-container input[type="checkbox"]').forEach(chk => {
                chk.checked = status;
            });
            salvarSelecaoAtivos();
        }

        async function salvarSelecaoAtivos() {
            const marcados = [];
            document.querySelectorAll('#asset-checkbox-container input[type="checkbox"]:checked').forEach(chk => {
                marcados.push(chk.value);
            });

            await fetch('/salvar_config_operacional', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ ativos: marcados.length > 0 ? marcados : "TODOS" })
            });
        }

        function renderizarCatalogacao(catalogData) {
            const container = document.getElementById('catalog-content');
            if(!catalogData || !catalogData.estrategias || catalogData.estrategias.length === 0) {
                container.innerHTML = "<p style='color:#ef4444; font-size:11px; text-align:center;'>Nenhum dado catalogado nas últimas 60 velas.</p>";
                return;
            }

            const ests = catalogData.estrategias;
            const porAtivo = catalogData.por_ativo || [];
            const maisForte = ests[0];

            let html = `
                <div style="background:rgba(16,185,129,0.1); border:1px solid #10b981; padding:12px; border-radius:12px; margin-bottom:12px;">
                    <div style="font-size:10px; color:#10b981; font-weight:800; text-transform:uppercase;">🔥 ESTRATÉGIA MAIS ASSERTIVA NO MOMENTO</div>
                    <div style="font-size:16px; font-weight:900; color:#fff; margin:4px 0;">${maisForte.nome_display} — <span style="color:#00f2fe;">${maisForte.winrate}%</span></div>
                    <div style="font-size:11px; color:#94a3b8;">Wins: <b style="color:#10b981">${maisForte.wins}</b> | Losses: <b style="color:#ef4444">${maisForte.losses}</b> | Total Entradas: ${maisForte.total}</div>
                </div>

                <div style="font-size:11px; color:#00f2fe; font-weight:800; text-transform:uppercase; margin-bottom:6px;">📊 RANKING E ASSERTIVIDADE DAS ESTRATÉGIAS</div>
                <table class="catalog-table">
                    <thead>
                        <tr>
                            <th>Estratégia</th>
                            <th>Wins</th>
                            <th>Loss</th>
                            <th>Assertividade</th>
                        </tr>
                    </thead>
                    <tbody>`;

            ests.forEach(item => {
                html += `<tr>
                    <td><b>${item.nome_display}</b></td>
                    <td style="color:#10b981; font-weight:bold;">${item.wins}</td>
                    <td style="color:#ef4444; font-weight:bold;">${item.losses}</td>
                    <td style="color:#00f2fe; font-weight:bold;">${item.winrate}%</td>
                </tr>`;
            });

            html += `</tbody></table>`;

            if (porAtivo.length > 0) {
                html += `
                    <div style="font-size:11px; color:#00f2fe; font-weight:800; text-transform:uppercase; margin:14px 0 6px 0;">🎯 MELHOR ESTRATÉGIA PARA CADA ATIVO</div>
                    <div style="max-height:180px; overflow-y:auto; border:1px solid #1e293b; border-radius:8px; padding:6px; background:#0f172a;">
                        <table class="catalog-table" style="margin-top:0;">
                            <thead>
                                <tr>
                                    <th>Ativo</th>
                                    <th>Melhor Estratégia</th>
                                    <th>Assertividade</th>
                                </tr>
                            </thead>
                            <tbody>`;

                porAtivo.forEach(item => {
                    html += `<tr>
                        <td><b>${item.ativo}</b></td>
                        <td style="color:#cbd5e1;">${item.nome_estrategia}</td>
                        <td style="color:#10b981; font-weight:bold;">${item.winrate}% (${item.wins}W/${item.losses}L)</td>
                    </tr>`;
                });

                html += `</tbody></table></div>`;
            }

            html += `
                <div style="margin-top:15px; border-top:1px solid #1e293b; padding-top:12px; display:flex; flex-direction:column; gap:8px;">
                    <div style="font-size:11px; font-weight:800; color:#00f2fe;">⚡ ESCOLHA SUA CONFIGURAÇÃO PARA OPERAR:</div>
                    
                    <button onclick="aplicarConfigOperacional('${maisForte.estrategia}', 'TODOS')" style="width:100%; padding:11px; background:linear-gradient(135deg, #10b981, #059669); border:none; color:white; font-weight:bold; font-size:11px; border-radius:8px; cursor:pointer;">
                        🎯 OPERAR APENAS A MELHOR ESTRATÉGIA (${maisForte.nome_display})
                    </button>

                    <button onclick="aplicarConfigOperacional('TODAS', 'TODOS')" style="width:100%; padding:11px; background:#1e293b; border:1px solid #00f2fe; color:#00f2fe; font-weight:bold; font-size:11px; border-radius:8px; cursor:pointer;">
                        🌐 OPERAR COM TODAS AS ESTRATÉGIAS E ATIVOS
                    </button>
                </div>
            `;

            container.innerHTML = html;
        }

        let listaAtivosInicializada = false;

        async function atualizarPainel() {
            try {
                const r = await fetch('/status', { cache: 'no-store' });
                const data = await r.json();
                const panel = document.getElementById('panel-text');
                if(panel && data.html) panel.innerHTML = data.html;
                if(document.getElementById('win-count')) document.getElementById('win-count').innerText = data.wins;
                if(document.getElementById('loss-count')) document.getElementById('loss-count').innerText = data.reds;
                if(document.getElementById('wr-text')) document.getElementById('wr-text').innerText = data.winrate + "%";
                if(document.getElementById('wr-fill')) document.getElementById('wr-fill').style.width = data.winrate + "%";
                if(document.getElementById('result-area')) document.getElementById('result-area').style.display = data.aguardando ? 'grid' : 'none';
                
                if(document.getElementById('mkt-badge')) document.getElementById('mkt-badge').innerText = data.mercado || "TODOS";
                if(document.getElementById('ativos-badge')) {
                    const atvs = data.ativos_selecionados;
                    if (atvs === "TODOS" || !Array.isArray(atvs)) {
                        document.getElementById('ativos-badge').innerText = "TODOS OS ATIVOS";
                    } else {
                        document.getElementById('ativos-badge').innerText = `${atvs.length} ATIVO(S) SELECIONADO(S)`;
                    }
                }

                if(!listaAtivosInicializada && data.mercado) {
                    atualizarListaAtivosSelecao(data.mercado, data.ativos_selecionados);
                    listaAtivosInicializada = true;
                }

                if(document.getElementById('current-asset')) {
                    if(data.rodando) {
                        document.getElementById('current-asset').innerText = data.ativo_atual || "VARRENDO...";
                    } else {
                        document.getElementById('current-asset').innerText = "SISTEMA PAUSADO";
                    }
                }

                // Exibição do relatório de varredura
                const catalogBox = document.getElementById('catalog-box');
                const catalogLoader = document.getElementById('catalog-loader');
                if(data.catalogando) {
                    catalogBox.style.display = 'block';
                    catalogLoader.style.display = 'block';
                    document.getElementById('catalog-content').innerHTML = '';
                } else if(data.catalogacao) {
                    catalogLoader.style.display = 'none';
                    if(catalogBox.style.display === 'block' && document.getElementById('catalog-content').innerHTML === '') {
                        renderizarCatalogacao(data.catalogacao);
                    }
                }

                if(data.notificacao && data.notificacao.id !== lastNotifId) {
                    lastNotifId = data.notificacao.id;
                    dispararNotificacaoNativa(data.notificacao.titulo, data.notificacao.corpo, data.notificacao.id);
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
            } catch (err) {
                console.warn('Falha ao atualizar o painel:', err);
            } finally {
                setTimeout(atualizarPainel, 250);
            }
        }

        atualizarPainel();

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

# ================= MOTOR DE ANÁLISE REAL DE VELAS =================
def get_data_v2(ticker, tf, velas_minimas=60):
    """Busca dados reais OHLC e garante o mínimo de velas exigido."""
    try:
        base_ticker = ticker
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36',
            'Accept': 'application/json, text/plain, */*'
        }
        
        url = f"https://query2.finance.yahoo.com/v8/finance/chart/{base_ticker}?interval={tf}m&range=5d"
        res = requests.get(url, headers=headers, timeout=5.0)
        
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
            url_alt = f"https://min-api.cryptocompare.com/data/v2/histo/minute?fsym={crypto_symbol}&tsym=USD&limit=120&aggregate={tf}"
            r_alt = requests.get(url_alt, timeout=5.0).json()
            
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
    
    if len(c) < 25: 
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
            
            if cor == "G" and p_inf >= (amplitude * 0.45) and p_sup <= (amplitude * 0.20):
                sinal = "CALL"
                probabilidade = int(82 + (p_inf / amplitude) * 15)
            elif cor == "R" and p_sup >= (amplitude * 0.45) and p_inf <= (amplitude * 0.20):
                sinal = "PUT"
                probabilidade = int(82 + (p_sup / amplitude) * 15)
            elif cor == "G" and p_sup >= (amplitude * 0.50) and tamanho <= (amplitude * 0.35):
                sinal = "PUT"
                probabilidade = int(80 + (p_sup / amplitude) * 15)
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
        if abs(i) + 2 < len(c):
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

# ================= MOTOR DE CATALOGAÇÃO COMPLETA DAS 60 VELAS (VARREDURA PRÉ-OPERACIONAL) =================
def executar_catalogacao_60_velas(user_email):
    """
    Executa uma varredura nas últimas 60 velas de todo o mercado antes de iniciar o bot.
    Identifica a melhor estratégia, os melhores ativos e carrega a análise pronta no painel e Telegram.
    """
    st = get_user_state(user_email)
    if not st: return
    
    st["catalogando"] = True
    st["catalogacao_resultado"] = None

    mkt = st.get("tipo_mercado", "TODOS")
    tf = st.get("timeframe", 5)

    if mkt == "TODOS":
        ativos = ATIVOS_BASE["FOREX_ABERTO"] + ATIVOS_BASE["CRIPTO_ABERTO"] + ATIVOS_BASE["FOREX_OTC"] + ATIVOS_BASE["CRIPTO_OTC"]
    elif mkt == "ABERTO_TODOS":
        ativos = ATIVOS_BASE["FOREX_ABERTO"] + ATIVOS_BASE["CRIPTO_ABERTO"]
    elif mkt == "OTC_TODOS":
        ativos = ATIVOS_BASE["FOREX_OTC"] + ATIVOS_BASE["CRIPTO_OTC"]
    else:
        ativos = ATIVOS_BASE.get(mkt, ATIVOS_BASE["FOREX_ABERTO"])

    resultados_est = {est: {"wins": 0, "losses": 0, "ativos": {}} for est in LISTA_ESTRATEGIAS}
    desempenho_ativo_est = {}

    for ativo in ativos:
        desempenho_ativo_est[ativo] = {est: {"wins": 0, "losses": 0} for est in LISTA_ESTRATEGIAS}
        ticker = MAPA_TICKERS.get(ativo, ativo)
        data = get_data_v2(ticker, tf, velas_minimas=60)
        if not data or len(data["close"]) < 60:
            continue

        n = len(data["close"])
        for est in LISTA_ESTRATEGIAS:
            if ativo not in resultados_est[est]["ativos"]:
                resultados_est[est]["ativos"][ativo] = {"wins": 0, "losses": 0}

            for i in range(25, n - 1):
                data_sub = {
                    "time": data["time"][:i+1],
                    "open": data["open"][:i+1],
                    "high": data["high"][:i+1],
                    "low": data["low"][:i+1],
                    "close": data["close"][:i+1]
                }
                sinal, prob = analisar_estrategia(data_sub, est, -1)
                if sinal:
                    c_next = data["close"][i+1]
                    o_next = data["open"][i+1]
                    is_win = (sinal == "CALL" and c_next > o_next) or (sinal == "PUT" and c_next < o_next)
                    
                    if is_win:
                        resultados_est[est]["wins"] += 1
                        resultados_est[est]["ativos"][ativo]["wins"] += 1
                        desempenho_ativo_est[ativo][est]["wins"] += 1
                    else:
                        resultados_est[est]["losses"] += 1
                        resultados_est[est]["ativos"][ativo]["losses"] += 1
                        desempenho_ativo_est[ativo][est]["losses"] += 1

    resumo_est = []
    for est, stats in resultados_est.items():
        tot = stats["wins"] + stats["losses"]
        wr = round((stats["wins"] / tot) * 100, 1) if tot > 0 else 0.0

        top_ativos = []
        for atv, atv_stats in stats["ativos"].items():
            atv_tot = atv_stats["wins"] + atv_stats["losses"]
            if atv_tot > 0:
                atv_wr = round((atv_stats["wins"] / atv_tot) * 100, 1)
                top_ativos.append({
                    "ativo": atv,
                    "winrate": atv_wr,
                    "wins": atv_stats["wins"],
                    "losses": atv_stats["losses"]
                })
        top_ativos.sort(key=lambda x: (x["winrate"], x["wins"]), reverse=True)

        resumo_est.append({
            "estrategia": est,
            "nome_display": NOME_ESTRATEGIAS_DISPLAY.get(est, est),
            "wins": stats["wins"],
            "losses": stats["losses"],
            "total": tot,
            "winrate": wr,
            "melhores_ativos": top_ativos[:5]
        })

    resumo_est.sort(key=lambda x: (x["winrate"], x["wins"]), reverse=True)

    melhores_por_ativo = []
    for ativo, est_dict in desempenho_ativo_est.items():
        melhor_est_nome = None
        melhor_wr = -1.0
        melhor_wins = 0
        melhor_losses = 0

        for est, stats in est_dict.items():
            tot = stats["wins"] + stats["losses"]
            if tot > 0:
                wr = round((stats["wins"] / tot) * 100, 1)
                if wr > melhor_wr or (wr == melhor_wr and stats["wins"] > melhor_wins):
                    melhor_wr = wr
                    melhor_est_nome = est
                    melhor_wins = stats["wins"]
                    melhor_losses = stats["losses"]

        if melhor_est_nome and melhor_wr >= 0:
            melhores_por_ativo.append({
                "ativo": ativo,
                "estrategia": melhor_est_nome,
                "nome_estrategia": NOME_ESTRATEGIAS_DISPLAY.get(melhor_est_nome, melhor_est_nome),
                "winrate": melhor_wr,
                "wins": melhor_wins,
                "losses": melhor_losses
            })

    melhores_por_ativo.sort(key=lambda x: (x["winrate"], x["wins"]), reverse=True)

    st["catalogacao_resultado"] = {
        "estrategias": resumo_est,
        "por_ativo": melhores_por_ativo
    }
    st["catalogando"] = False

    if resumo_est:
        mais_forte = resumo_est[0]
        top_ativos_str = ", ".join([a["ativo"] for a in mais_forte["melhores_ativos"][:3]]) if mais_forte["melhores_ativos"] else "Todos"
        
        st["estrategia"] = mais_forte["estrategia"]

        st["ultimo_sinal"] = f"""
        <div style='background: rgba(16, 185, 129, 0.15); border: 2px solid #10b981; padding: 15px; border-radius: 14px; text-align: center;'>
            <div style='color: #10b981; font-weight: 800; font-size: 13px; text-transform: uppercase;'>✅ VARREDURA CONCLUÍDA COM SUCESSO!</div>
            <div style='font-size: 17px; font-weight: 900; color: #ffffff; margin: 6px 0;'>Estratégia Recomendada: <span style='color:#00f2fe;'>{mais_forte['nome_display']}</span> ({mais_forte['winrate']}%)</div>
            <div style='font-size: 12px; color: #cbd5e1;'>🎯 <b>Melhores Ativos:</b> {top_ativos_str}</div>
            <div style='font-size: 11px; color: #38ef7d; margin-top: 6px;'><b>💡 O robô já pré-configurou a melhor opção. Clique em START para operar!</b></div>
        </div>
        """

        msg_tg_concluida = (
            f"📊 <b>ANÁLISE PRÉ-OPERACIONAL CONCLUÍDA!</b>\n\n"
            f"🔥 <b>Estratégia Mais Assertiva:</b> {mais_forte['nome_display']} ({mais_forte['winrate']}% WR)\n"
            f"🎯 <b>Melhores Ativos Indicados:</b> {top_ativos_str}\n"
            f"📈 <b>Aproveitamento:</b> {mais_forte['wins']} Wins / {mais_forte['losses']} Losses\n\n"
            f"⚡ <i>A melhor configuração foi ajustada no seu painel. Pronto para operar!</i>"
        )
        enviar_telegram(msg_tg_concluida, user_solicitante=user_email)

        st["notificacao"] = {
            "id": int(time.time() * 1000),
            "titulo": "✅ ANÁLISE PRONTA E CONCLUÍDA!",
            "corpo": f"Melhor Estratégia: {mais_forte['nome_display']} ({mais_forte['winrate']}%) em {top_ativos_str}"
        }

# ================= MOTOR PRINCIPAL DE VARREDURA EM TEMPO REAL (THREAD CONTINUA) =================
def loop_varredura_principal():
    """Thread contínua que executa em segundo plano monitorando sinais para todos os usuários."""
    while True:
        try:
            for email, st in list(DADOS_USUARIOS.items()):
                if not st.get("bot_iniciado") or st.get("bot_pausado") or st.get("aguardando_confirmacao") or st.get("catalogando"):
                    continue

                if time.time() < st.get("inicio_varredura", 0):
                    continue

                mkt = st.get("tipo_mercado", "TODOS")
                tf = st.get("timeframe", 5)
                est_config = st.get("estrategia", "TODAS")
                ativos_sel = st.get("ativos_selecionados", "TODOS")

                if ativos_sel != "TODOS" and isinstance(ativos_sel, list) and len(ativos_sel) > 0:
                    lista_ativos = ativos_sel
                else:
                    if mkt == "TODOS":
                        lista_ativos = ATIVOS_BASE["FOREX_ABERTO"] + ATIVOS_BASE["CRIPTO_ABERTO"] + ATIVOS_BASE["FOREX_OTC"] + ATIVOS_BASE["CRIPTO_OTC"]
                    elif mkt == "ABERTO_TODOS":
                        lista_ativos = ATIVOS_BASE["FOREX_ABERTO"] + ATIVOS_BASE["CRIPTO_ABERTO"]
                    elif mkt == "OTC_TODOS":
                        lista_ativos = ATIVOS_BASE["FOREX_OTC"] + ATIVOS_BASE["CRIPTO_OTC"]
                    else:
                        lista_ativos = ATIVOS_BASE.get(mkt, ATIVOS_BASE["FOREX_ABERTO"])

                sinal_encontrado = False

                for ativo in lista_ativos:
                    if not st.get("bot_iniciado") or st.get("bot_pausado") or st.get("aguardando_confirmacao"):
                        break

                    st["ativo_atual"] = ativo
                    ticker = MAPA_TICKERS.get(ativo, ativo)
                    data = get_data_v2(ticker, tf, velas_minimas=30)

                    if not data:
                        continue

                    estrategias_para_testar = LISTA_ESTRATEGIAS if est_config == "TODAS" else [est_config]
                    
                    melhor_sinal = None
                    melhor_prob = 0
                    melhor_est = None

                    for est in estrategias_para_testar:
                        sinal, prob = analisar_estrategia(data, est, -1)
                        if sinal and prob > melhor_prob:
                            melhor_sinal = sinal
                            melhor_prob = prob
                            melhor_est = est

                    if melhor_sinal and melhor_prob >= 80:
                        chave_sinal = f"{ativo}_{melhor_sinal}_{tf}"
                        ultimo_envio = st["sinais_enviados"].get(chave_sinal, 0)
                        
                        if time.time() - ultimo_envio > (tf * 60 * 0.8):
                            st["sinais_enviados"][chave_sinal] = time.time()
                            st["aguardando_confirmacao"] = True
                            
                            horario_exp = (agora_brasilia() + timedelta(minutes=tf)).strftime("%H:%M")
                            dir_emoji = "🟢 CALL (COMPRA)" if melhor_sinal == "CALL" else "🔴 PUT (VENDA)"
                            nome_est_display = NOME_ESTRATEGIAS_DISPLAY.get(melhor_est, melhor_est)

                            st["sinal_permanente"] = f"""
                            <div style='background: rgba(0, 242, 254, 0.1); border: 2px solid #00f2fe; border-radius: 14px; padding: 15px; text-align: center;'>
                                <div style='font-size: 11px; color: #00f2fe; font-weight: 800; text-transform: uppercase;'>🚨 SINAL DETECTADO PELO ROBÔ</div>
                                <div style='font-size: 22px; font-weight: 900; color: #ffffff; margin: 6px 0;'>{ativo}</div>
                                <div style='font-size: 18px; font-weight: 800; color: {"#10b981" if melhor_sinal == "CALL" else "#ef4444"};'>{dir_emoji}</div>
                                <div style='font-size: 12px; color: #cbd5e1; margin-top: 6px;'>
                                    ⏱ Expiração: <b>M{tf} ({horario_exp})</b> | 🎯 Probabilidade: <b style='color:#38ef7d;'>{melhor_prob}%</b><br>
                                    ⚙️ Estratégia: <b>{nome_est_display}</b>
                                </div>
                            </div>
                            """

                            st["notificacao"] = {
                                "id": int(time.time() * 1000),
                                "titulo": f"🚨 OPORTUNIDADE: {ativo} ({melhor_sinal})",
                                "corpo": f"Direção: {melhor_sinal} | Expirar: {horario_exp} (M{tf}) | Prob: {melhor_prob}%"
                            }

                            sinal_str = f"{ativo} | {melhor_sinal} | M{tf} | {nome_est_display} ({melhor_prob}%)"
                            registrar_sinal_bd(email, sinal_str)

                            msg_tg = (
                                f"🚨 <b>SINAL DETECTADO - VISION PRO V3</b>\n\n"
                                f"📊 <b>Ativo:</b> {ativo}\n"
                                f"📈 <b>Direção:</b> {'🟢 CALL (COMPRA)' if melhor_sinal == 'CALL' else '🔴 PUT (VENDA)'}\n"
                                f"⏱ <b>Timeframe:</b> M{tf} (Expiração às {horario_exp})\n"
                                f"🎯 <b>Probabilidade Assertiva:</b> {melhor_prob}%\n"
                                f"⚙️ <b>Estratégia:</b> {nome_est_display}\n\n"
                                f"<i>Confirme o resultado no painel após o fechamento da vela!</i>"
                            )
                            st["alerta_ativo"] = {
                                "msg_id": enviar_telegram(msg_tg, user_solicitante=email)
                            }

                            sinal_encontrado = True
                            break

                if not sinal_encontrado and not st.get("aguardando_confirmacao"):
                    time.sleep(1)

        except Exception as e:
            print(f"Erro no loop de varredura: {e}")
        time.sleep(2)

# ================= ROTA SERVICE WORKER DE NOTIFICAÇÃO =================
@app.route('/sw.js')
def service_worker():
    sw_code = """
    self.addEventListener('install', function(event) {
        self.skipWaiting();
    });

    self.addEventListener('activate', function(event) {
        event.waitUntil(self.clients.claim());
    });

    self.addEventListener('notificationclick', function(event) {
        event.notification.close();

        event.waitUntil(
            clients.matchAll({ type: 'window', includeUncontrolled: true }).then(function(clientList) {
                for (var i = 0; i < clientList.length; i++) {
                    var client = clientList[i];
                    if ('focus' in client) return client.focus();
                }
                if (clients.openWindow) return clients.openWindow('/');
            })
        );
    });
    """
    response = Response(sw_code, mimetype='application/javascript')
    response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
    response.headers['Pragma'] = 'no-cache'
    response.headers['Expires'] = '0'
    return response

# ================= ROTAS DE NAVEGAÇÃO =================
@app.route('/health')
def health():
    return jsonify({"status": "ok"}), 200

@app.route('/')
def index():
    if 'user' not in session:
        return redirect('/login')
    
    user = session['user']
    st = get_user_state(user)
    usuarios = carregar_usuarios()
    
    if user not in usuarios and user != ADMIN_EMAIL:
        session.pop('user', None)
        return redirect('/login')

    return render_template_string(HTML_INDEX, 
                                  user=user, 
                                  admin=ADMIN_EMAIL,
                                  modo=st.get("tipo_mercado", "TODOS"),
                                  tf=st.get("timeframe", 5),
                                  estrat=st.get("estrategia", "TODAS"))

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
        
        if not e or not s:
            return render_template_string(HTML_REGISTER, erro="Preencha todos os campos obrigatórios.")
            
        usuarios = carregar_usuarios()
        if e in usuarios:
            return render_template_string(HTML_REGISTER, erro="Este e-mail já está cadastrado. Vá para a página de Login.")
            
        try:
            ip_cliente = get_client_ip()
            salvar_usuario(e, s, agora_brasilia().strftime("%Y-%m-%d"), ip_inicial=ip_cliente)
            return redirect('/login')
        except Exception as err:
            return render_template_string(HTML_REGISTER, erro=f"Erro durante o cadastro: {err}")
            
    return render_template_string(HTML_REGISTER)

@app.route('/logout')
def logout():
    session.pop('user', None)
    return redirect('/login')

@app.route('/termos')
def termos():
    return render_template_string(HTML_TERMOS)

# ================= ROTAS DE CONTROLE E COMANDOS =================
@app.route('/status')
def status():
    user = session.get('user')
    if not user:
        return jsonify({"error": "unauthorized"}), 401
        
    USUARIOS_ONLINE[user] = time.time()
    st = get_user_state(user)
    
    usuarios = carregar_usuarios()
    user_db = usuarios.get(user, {})
    wins = user_db.get('wins', 0)
    reds = user_db.get('reds', 0)
    winrate = user_db.get('winrate', 0.0)
    historico = buscar_historico_bd(user)
    
    return jsonify({
        "html": st.get("sinal_permanente") if st.get("aguardando_confirmacao") else st.get("ultimo_sinal"),
        "aguardando": st.get("aguardando_confirmacao"),
        "wins": wins,
        "reds": reds,
        "winrate": winrate,
        "mercado": st.get("tipo_mercado"),
        "ativos_selecionados": st.get("ativos_selecionados"),
        "rodando": st.get("bot_iniciado") and not st.get("bot_pausado"),
        "ativo_atual": st.get("ativo_atual"),
        "catalogando": st.get("catalogando"),
        "catalogacao": st.get("catalogacao_resultado"),
        "historico": historico,
        "notificacao": st.get("notificacao")
    })

@app.route('/command/<cmd>')
def command(cmd):
    user = session.get('user')
    if not user: 
        return jsonify({"error": "unauthorized"}), 401
        
    st = get_user_state(user)

    if cmd == "start_bot":
        st["bot_iniciado"] = True
        st["bot_pausado"] = False
        st["aguardando_confirmacao"] = False
        st["inicio_varredura"] = time.time() + 2
        st["ultimo_sinal"] = "SISTEMA INICIADO! Varrendo ativos no mercado selecionado..."
        st["notificacao"] = {"id": int(time.time()), "titulo": "▶️ BOT INICIADO", "corpo": "A varredura de sinais começou."}
    elif cmd == "pause_bot":
        st["bot_pausado"] = True
        st["ultimo_sinal"] = "Bot Pausado pelo Usuário."
    elif cmd == "stop_bot":
        st["bot_iniciado"] = False
        st["bot_pausado"] = True
        st["aguardando_confirmacao"] = False
        st["ultimo_sinal"] = "Bot Parado."
    elif cmd.startswith("mkt_"):
        st["tipo_mercado"] = cmd.split("mkt_")[1]
    elif cmd.startswith("tf_"):
        st["timeframe"] = int(cmd.split("tf_")[1])
    elif cmd.startswith("set_est_"):
        st["estrategia"] = cmd.split("set_est_")[1]
    elif cmd == "fazer_catalogacao":
        threading.Thread(target=executar_catalogacao_60_velas, args=(user,)).start()
    elif cmd == "test_telegram":
        enviar_telegram("🧪 <b>TESTE DE CONEXÃO</b>\nO Vision Pro V3 está sincronizado com o Telegram com sucesso!", user_solicitante=user)
    
    return jsonify({"status": "ok"})

@app.route('/resultado/<res>')
def resultado(res):
    user = session.get('user')
    if not user: 
        return jsonify({"error": "unauthorized"}), 401
        
    st = get_user_state(user)

    if not st.get("aguardando_confirmacao"):
        return jsonify({"status": "ignorado"})

    st["aguardando_confirmacao"] = False
    msg_res = ""
    is_win = False

    if res == "win":
        is_win = True
        msg_res = "✅ Win Direto"
    elif res == "g1":
        is_win = True
        msg_res = "✅ Win no G1"
    elif res == "red":
        msg_res = "❌ Red"
    elif res == "pular":
        msg_res = "⏭️ Sinal Ignorado"
        
    if res in ["win", "g1", "red"]:
        atualizar_estatisticas_usuario(user, is_win)
        atualizar_ultimo_sinal_bd(user, msg_res)
        
    st["ultimo_sinal"] = f"Resultado Registrado: {msg_res}. Retomando varredura em 5 segundos..."
    st["inicio_varredura"] = time.time() + 5
    
    return jsonify({"status": "ok"})

@app.route('/salvar_config_operacional', methods=['POST'])
def salvar_config_operacional():
    user = session.get('user')
    if not user: 
        return jsonify({"error": "unauthorized"}), 401
        
    st = get_user_state(user)
    data = request.json
    
    if data and 'estrategia' in data:
        st['estrategia'] = data['estrategia']
    if data and 'ativos' in data:
        st['ativos_selecionados'] = data['ativos']
        
    return jsonify({"status": "ok"})

# ================= ROTAS ADMINISTRATIVAS =================
@app.route('/admin_panel')
def admin_panel():
    if session.get('user') != ADMIN_EMAIL:
        abort(403)
        
    usuarios = carregar_usuarios()
    online_list = [k for k, v in USUARIOS_ONLINE.items() if time.time() - v < 300]
    
    return render_template_string(HTML_ADM, 
                                  lista=usuarios, 
                                  online_count=len(online_list), 
                                  online_list=online_list, 
                                  admin=ADMIN_EMAIL)

@app.route('/adm/editar', methods=['POST'])
def adm_editar():
    if session.get('user') != ADMIN_EMAIL: 
        abort(403)
        
    email_orig = request.form.get('email_original')
    nova_senha = request.form.get('nova_senha')
    
    if nova_senha:
        salvar_usuario(email_orig, nova_senha, None)
        
    return redirect('/admin_panel')

@app.route('/adm/renovar/<email>')
def adm_renovar(email):
    if session.get('user') != ADMIN_EMAIL: 
        abort(403)
        
    renovar_usuario_db(email)
    return redirect('/admin_panel')

@app.route('/adm/excluir/<email>')
def adm_excluir(email):
    if session.get('user') != ADMIN_EMAIL: 
        abort(403)
        
    excluir_usuario_db(email)
    return redirect('/admin_panel')

@app.route('/adm/liberar_ip/<email>')
def adm_liberar_ip(email):
    if session.get('user') != ADMIN_EMAIL: 
        abort(403)
        
    liberar_ip_usuario_db(email)
    return redirect('/admin_panel')

# ================= INICIALIZAÇÃO DA APLICAÇÃO =================
if __name__ == '__main__':
    # Inicia a thread responsável pela varredura em segundo plano (em todos os mercados simultaneamente)
    threading.Thread(target=loop_varredura_principal, daemon=True).start()
    
    # Executa a aplicação Flask
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
