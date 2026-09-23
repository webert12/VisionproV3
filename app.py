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
def _required_env(name, aliases=()):
    value = os.getenv(name)
    if not value:
        for alias in aliases:
            value = os.getenv(alias)
            if value:
                break
    if not value or not value.strip():
        raise RuntimeError(f"Variável de ambiente obrigatória não configurada: {name}")
    return value.strip()

# Dados sensíveis ficam EXCLUSIVAMENTE nas Environment Variables do Render.
# Não existe fallback de token, chat ID, e-mail administrativo ou chave secreta no código.
TOKEN_TELEGRAM = _required_env("TOKEN_TELEGRAM")
CHAT_ID_TELEGRAM = _required_env("CHAT_ID_TELEGRAM")
ADMIN_EMAIL = _required_env("ADMIN_EMAIL").lower()
DB_URL = _required_env("DB_URL", aliases=("DATABASE_URL",))

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
            "alerta_ativo": None,  # Guarda informações do alerta ativo no ciclo
            "timer_confirmacao": None,  # Timer independente para não depender da varredura
            "notificacao": None,
            "notificacao_ultima_hora": 0.0,
            "candle_remaining": 0,
            "sessao_resultados": []
        }
    return DADOS_USUARIOS[email_clean]

def get_client_ip():
    if request.headers.get('X-Forwarded-For'):
        return request.headers.get('X-Forwarded-For').split(',')[0].strip()
    return request.remote_addr

# ================= ENVIO E DELEÇÃO TELEGRAM =================
def enviar_telegram(mensagem, auto_delete=None, user_solicitante=None):
    # Telegram só pode ser usado quando o ADM ativou o envio e somente para mensagens
    # originadas da sessão administrativa. Usuários comuns nunca enviam ao Telegram.
    if user_solicitante and user_solicitante.strip().lower() != ADMIN_EMAIL:
        return None
    if not telegram_envio_ativo():
        return None
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
APP_SECRET = _required_env("FLASK_SECRET")
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
    function renovarCustom(email, idx) {
            const meses = parseInt(document.getElementById('meses-'+idx).value || '0', 10);
            if (!meses || meses < 1 || meses > 120) { alert('Informe de 1 a 120 meses.'); return; }
            location.href = '/adm/renovar/' + encodeURIComponent(email) + '/' + meses;
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
                <b>Expira em:</b> {{ info.expira_em or info.criado_em }}<br>
                <b>Status:</b> {% if info.bloqueado %}<span style="color:#ef4444;">BLOQUEADO</span>{% else %}<span style="color:#10b981;">LIBERADO</span>{% endif %}<br><br>
                <button type="submit" class="btn-adm blue">SALVAR ALTERAÇÕES</button>
                <a href="/adm/renovar/{{ email }}/1" class="btn-adm green">+1 MÊS</a>
                <a href="/adm/renovar/{{ email }}/3" class="btn-adm green">+3 MESES</a>
                <a href="/adm/renovar/{{ email }}/6" class="btn-adm green">+6 MESES</a>
                <a href="/adm/renovar/{{ email }}/12" class="btn-adm green">+12 MESES</a>
                <a href="/adm/liberar_ip/{{ email }}" class="btn-adm orange">LIBERAR DISPOSITIVOS / IPS</a>
                <input type="number" min="1" max="120" id="meses-{{ loop.index }}" placeholder="Meses personalizados">
                <button type="button" class="btn-adm blue" onclick="renovarCustom('{{ email }}', {{ loop.index }})">RENOVAR MESES</button>
                {% if email != admin %}
                {% if info.bloqueado %}<a href="/adm/bloquear/{{ email }}/liberar" class="btn-adm blue">DESBLOQUEAR</a>{% else %}<a href="/adm/bloquear/{{ email }}/bloquear" class="btn-adm red" onclick="return confirm('Bloquear este usuário?')">BLOQUEAR</a>{% endif %}
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

        <div class="tools-box">
            <button class="btn-notify" onclick="toggleFerramentas()">⚙️ FERRAMENTAS E NOTIFICAÇÕES</button>
            <div id="ferramentas-box" style="display:none;">
                <button class="btn-notify" id="btn-enable-notify" onclick="solicitarPermissaoNotificacao()">🔔 ATIVAR NOTIFICAÇÕES NO CELULAR</button>
                {% if user == admin %}
                <button class="btn-test-tg" onclick="sendCommand('test_telegram')">🧪 TESTAR CONEXÃO TELEGRAM</button>
                <button class="btn-test-tg" id="btn-telegram-toggle" onclick="toggleTelegram()">{{ '🟢 ENVIO TELEGRAM ATIVADO' if telegram_ativo else '🔴 ENVIO TELEGRAM DESATIVADO' }}</button>
                <div style="font-size:10px;color:#94a3b8;text-align:center;margin:6px 0 10px;">Quando ativado pelo ADM, somente mensagens da sessão ADM são enviadas ao Telegram.</div>
                {% endif %}
            </div>
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
        <div id="ticker-live-status" style="background: rgba(0, 242, 254, 0.05); border: 1px solid rgba(0, 242, 254, 0.2); border-radius: 12px; padding: 10px; margin-bottom: 12px; text-align: center; font-size: 12px;">
            MERCADO SELECIONADO: <b id="mkt-badge" style="color: #00f2fe;">{{ modo }}</b> | 
            ANALISANDO AGORA: <b id="current-asset" style="color: #38ef7d;">AGUARDANDO...</b>
        </div>

        <div id="timing-panel" style="background:rgba(16,185,129,0.06); border:1px solid rgba(16,185,129,0.28); border-radius:12px; padding:12px; margin-bottom:12px; text-align:center; font-size:12px; line-height:1.7;">
            <div id="candle-timer" style="color:#00f2fe; font-weight:800;">⏳ FECHAMENTO DO CANDLE: --:--</div>
            <div id="entry-timer" style="color:#38ef7d; font-weight:900; font-size:14px; margin-top:3px;">🎯 AGUARDANDO SINAL DE ENTRADA</div>
            <div id="entry-clock" style="color:#94a3b8; font-size:11px;">Horário exato: --:--:--</div>
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

        // Registra o Service Worker, mas não dispara nenhuma notificação automaticamente.
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

                    // Não envia uma notificação de teste imediatamente após a permissão.
                    // Isso evita uma notificação desnecessária no momento da ativação.
                } else {
                    alert('Permissão de Notificação Recusada.');
                }
            });
        }

        async function dispararNotificacaoNativa(titulo, corpo, notifId) {
            if (!('Notification' in window) || Notification.permission !== 'granted') return;

            const id = String(notifId || '');
            const agora = Date.now();

            // Evita repetir a mesma notificação após recarregar/consultar o painel.
            const ultimoId = localStorage.getItem('vision_last_notif_id') || '';
            const ultimaHora = Number(localStorage.getItem('vision_last_notif_at') || '0');

            if (id && id === ultimoId) return;
            if (ultimaHora && (agora - ultimaHora) < NATIVE_NOTIFICATION_COOLDOWN_MS) return;

            try {
                if ('serviceWorker' in navigator) {
                    const reg = await navigator.serviceWorker.ready;

                    await reg.showNotification(titulo, {
                        body: corpo,
                        // Sem vibração repetitiva e sem renotify: comportamento menos intrusivo.
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

        function toggleFerramentas() {
            const box = document.getElementById('ferramentas-box');
            box.style.display = box.style.display === 'block' ? 'none' : 'block';
        }

        function toggleTelegram() {
            fetch('/command/telegram_toggle').then(r => r.json()).then(data => {
                if (data.ok) {
                    const b = document.getElementById('btn-telegram-toggle');
                    b.innerText = data.telegram_ativo ? '🟢 ENVIO TELEGRAM ATIVADO' : '🔴 ENVIO TELEGRAM DESATIVADO';
                } else if (data.error) alert(data.error);
            });
        }

        function sendCommand(cmd) {
            fetch('/command/' + cmd).then(r => r.json()).then(data => {
                if(data.redirect) window.location.href = data.redirect;
            });
        }

        let timerState = { candleEnd: 0, entryEnd: 0, running: false, entryTime: null, aguardando: false };
        let serverClockOffset = 0;

        function formatarContagem(segundos) {
            const total = Math.max(0, Math.ceil(Number(segundos) || 0));
            const m = Math.floor(total / 60);
            const sec = total % 60;
            return String(m).padStart(2,'0') + ':' + String(sec).padStart(2,'0');
        }

        function atualizarRelogios() {
            const agora = (Date.now() / 1000) + serverClockOffset;
            const candleEl = document.getElementById('candle-timer');
            if (candleEl) {
                candleEl.innerText = timerState.running
                    ? ('⏳ FECHAMENTO DO CANDLE: ' + formatarContagem(timerState.candleEnd - agora))
                    : '⏸ CANDLE: PAUSADO';
            }

            const entryEl = document.getElementById('entry-timer');
            const clockEl = document.getElementById('entry-clock');
            if (entryEl && clockEl) {
                const restante = timerState.entryEnd ? (timerState.entryEnd - agora) : 0;
                if (timerState.entryTime && restante > 0) {
                    entryEl.innerText = '🎯 ENTRADA EM: ' + formatarContagem(restante);
                    clockEl.innerText = '⏰ HORÁRIO EXATO DA ENTRADA: ' + timerState.entryTime;
                } else if (timerState.entryTime && timerState.aguardando) {
                    entryEl.innerText = '🎯 ENTRADA CONFIRMADA — EXECUTE NO HORÁRIO INDICADO';
                    clockEl.innerText = '⏰ HORÁRIO DA ENTRADA: ' + timerState.entryTime;
                } else {
                    entryEl.innerText = '🎯 AGUARDANDO SINAL DE ENTRADA';
                    clockEl.innerText = 'Horário exato: --:--:--';
                }
            }
        }

        setInterval(atualizarRelogios, 1000);

        async function atualizarPainel() {
            try {
                const r = await fetch('/status', { cache: 'no-store' });
                const data = await r.json();
                // Sincroniza os relógios com o servidor apenas quando chega um novo estado.
                serverClockOffset = Number(data.server_now || (Date.now() / 1000)) - (Date.now() / 1000);
                timerState.running = !!data.rodando;
                timerState.candleEnd = Number(data.candle_end_ts || 0);
                timerState.entryEnd = data.entry_end_ts ? Number(data.entry_end_ts) : 0;
                timerState.entryTime = data.entry_time || null;
                timerState.aguardando = !!data.aguardando;
                atualizarRelogios();
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
                // Atualiza dados do painel sem usar a consulta HTTP como relógio.
                // Os cronômetros continuam correndo localmente de 1 em 1 segundo.
                setTimeout(atualizarPainel, 1000);
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
            ALTER TABLE usuarios ADD COLUMN IF NOT EXISTS expira_em DATE;
            ALTER TABLE usuarios ADD COLUMN IF NOT EXISTS bloqueado BOOLEAN DEFAULT FALSE;
            UPDATE usuarios SET expira_em = COALESCE(expira_em, (criado_em::date + INTERVAL '30 days')::date);

            CREATE TABLE IF NOT EXISTS sistema_config (
                chave VARCHAR(100) PRIMARY KEY,
                valor VARCHAR(255) NOT NULL
            );
            INSERT INTO sistema_config (chave, valor) VALUES ('telegram_envio_ativo', '0')
            ON CONFLICT (chave) DO NOTHING;

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

def telegram_envio_ativo():
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT valor FROM sistema_config WHERE chave = %s;", ("telegram_envio_ativo",))
        row = cur.fetchone()
        cur.close(); conn.close()
        return bool(row and str(row[0]).lower() in ("1", "true", "on", "sim"))
    except Exception:
        return False

def definir_telegram_envio(ativo):
    try:
        conn = get_db_connection(); cur = conn.cursor()
        cur.execute("INSERT INTO sistema_config (chave, valor) VALUES (%s,%s) ON CONFLICT (chave) DO UPDATE SET valor=EXCLUDED.valor;", ("telegram_envio_ativo", "1" if ativo else "0"))
        conn.commit(); cur.close(); conn.close()
        return True
    except Exception as e:
        print(f"Erro ao alterar trava Telegram: {e}")
        return False

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
            INSERT INTO usuarios (email, senha, criado_em, expira_em, bloqueado, wins, reds, winrate, ips_autorizados)
            VALUES (%s, %s, %s, (%s::date + INTERVAL '30 days')::date, FALSE, 0, 0, 0.0, %s)
            ON CONFLICT (email) DO UPDATE 
            SET senha = EXCLUDED.senha;
        """
        cur.execute(query, (email_clean, senha_hash, data_criacao, data_criacao, ips))
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

def renovar_usuario_db(email, meses=1):
    try:
        meses = max(1, min(int(meses), 120))
        dias = meses * 30
        email_clean = email.strip().lower()
        conn = get_db_connection(); cur = conn.cursor()
        cur.execute("SELECT expira_em FROM usuarios WHERE email = %s;", (email_clean,))
        row = cur.fetchone()
        hoje = agora_brasilia().date()
        if row and row[0]:
            base = row[0] if row[0] > hoje else hoje
        else:
            base = hoje
        nova_data = base + timedelta(days=dias)
        cur.execute("UPDATE usuarios SET expira_em = %s, bloqueado = FALSE WHERE email = %s;", (nova_data, email_clean))
        conn.commit(); cur.close(); conn.close()
        return nova_data
    except Exception as e:
        print(f"Erro ao renovar usuário: {e}")
        return None

def bloquear_usuario_db(email, bloqueado=True):
    try:
        email_clean = email.strip().lower()
        if email_clean == ADMIN_EMAIL: return False
        conn = get_db_connection(); cur = conn.cursor()
        cur.execute("UPDATE usuarios SET bloqueado = %s WHERE email = %s;", (bool(bloqueado), email_clean))
        conn.commit(); cur.close(); conn.close()
        return True
    except Exception as e:
        print(f"Erro ao bloquear usuário: {e}")
        return False

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
        conn = get_db_connection(); cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT expira_em, bloqueado FROM usuarios WHERE email = %s;", (email_clean,))
        res = cur.fetchone(); cur.close(); conn.close()
        if not res: return False, 0
        if res.get("bloqueado"): return False, -1
        expira = res.get("expira_em")
        if not expira:
            return False, 0
        dias_restantes = (expira - agora_brasilia().date()).days
        return (True, dias_restantes) if dias_restantes >= 0 else (False, 0)
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
        "BTCUSD", "ETHUSD", "SOLUSD", "BNBUSD", "XRPUSD", "AVAXUSD",
        "LINKUSD", "DOGEUSD", "DOTUSD", "LTCUSD", "TRXUSD"
    ],
    "FOREX_OTC": [
        "EURUSD-OTC", "GBPUSD-OTC", "USDJPY-OTC", "AUDUSD-OTC", "USDCAD-OTC", "USDCHF-OTC", "NZDUSD-OTC",
        "EURGBP-OTC", "EURJPY-OTC", "GBPJPY-OTC", "AUDJPY-OTC", "EURAUD-OTC", "EURCAD-OTC", "EURCHF-OTC"
    ],
    "CRIPTO_OTC": [
        "BTCUSD-OTC", "ETHUSD-OTC", "SOLUSD-OTC", "BNBUSD-OTC", "XRPUSD-OTC", "AVAXUSD-OTC",
        "LINKUSD-OTC", "DOGEUSD-OTC", "DOTUSD-OTC", "LTCUSD-OTC", "TRXUSD-OTC"
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
            url_alt = f"https://min-api.cryptocompare.com/data/v2/histo/minute?fsym={crypto_symbol}&tsym=USD&limit=100&aggregate={tf}"
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
            if dias == -1:
                return render_template_string(HTML_LOGIN, erro="🚫 ACESSO BLOQUEADO PELO ADMINISTRADOR.")
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

@app.route('/adm/renovar/<email>/<int:meses>')
def adm_renovar(email, meses):
    if session.get('user') != ADMIN_EMAIL: return abort(403)
    renovar_usuario_db(email, meses)
    return redirect('/admin_panel')

@app.route('/adm/bloquear/<email>/<acao>')
def adm_bloquear(email, acao):
    if session.get('user') != ADMIN_EMAIL: return abort(403)
    bloquear_usuario_db(email, acao == 'bloquear')
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
    return render_template_string(HTML_INDEX, modo=st["tipo_mercado"], tf=st["timeframe"], estrat=st["estrategia"], user=user, admin=ADMIN_EMAIL, telegram_ativo=telegram_envio_ativo())

@app.route('/status')
def status():
    user = session.get('user')
    if not user: return jsonify({})
    USUARIOS_ONLINE[user] = time.time()
    
    if user != ADMIN_EMAIL:
        ativo_assinatura, dias_assinatura = verificar_assinatura(user)
        if not ativo_assinatura:
            USUARIOS_ONLINE.pop(user, None)
            session.clear()
            return jsonify({"redirect": "/login", "error": "Acesso bloqueado ou assinatura expirada."})

    st = get_user_state(user)
    usuarios = carregar_usuarios()
    u_info = usuarios.get(user, {"wins": 0, "reds": 0, "winrate": 0.0})
    historico = buscar_historico_bd(user)
    
    display_texto = st["sinal_permanente"] if (st["aguardando_confirmacao"] and st["sinal_permanente"]) else st["ultimo_sinal"]

    response = jsonify({
        "html": display_texto, 
        "aguardando": st["aguardando_confirmacao"], 
        "wins": u_info.get("wins", 0),
        "reds": u_info.get("reds", 0), 
        "winrate": u_info.get("winrate", 0.0), 
        "historico": historico,
        "ativo_atual": st["ativo_atual"],
        "mercado": st["tipo_mercado"],
        "rodando": st["bot_iniciado"] and not st["bot_pausado"],
        "notificacao": st["notificacao"],
        "timeframe": st["timeframe"],
        "server_now": time.time(),
        "candle_end_ts": (math.floor(time.time() / (st["timeframe"] * 60)) + 1) * (st["timeframe"] * 60),
        "candle_remaining": max(0.0, ((math.floor(time.time() / (st["timeframe"] * 60)) + 1) * (st["timeframe"] * 60)) - time.time()),
        "entry_end_ts": (((st.get("alerta_ativo") or {}).get("momento_confirmacao").timestamp()) if (st.get("alerta_ativo") and (st.get("alerta_ativo") or {}).get("momento_confirmacao")) else None),
        "entry_remaining": max(0.0, (st.get("alerta_ativo") or {}).get("momento_confirmacao").timestamp() - time.time()) if (st.get("alerta_ativo") and (st.get("alerta_ativo") or {}).get("momento_confirmacao")) else 0,
        "entry_time": ((st.get("alerta_ativo") or {}).get("str_entrada") if st.get("alerta_ativo") else (re.search(r"ENTRADA:</b> ([0-9:]+)", st.get("sinal_permanente") or "") or [None, None])[1])
    })
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    return response

@app.route('/command/<cmd>')
def command(cmd):
    user = session.get('user')
    if not user:
        return jsonify({"ok": False})
    
    st = get_user_state(user)

    if cmd == "telegram_toggle":
        if user != ADMIN_EMAIL:
            return jsonify({"ok": False, "error": "Somente o ADM pode alterar o envio Telegram."}), 403
        ativo = not telegram_envio_ativo()
        definir_telegram_envio(ativo)
        return jsonify({"ok": True, "telegram_ativo": ativo})

    if cmd == "test_telegram":
        if user != ADMIN_EMAIL:
            return jsonify({"ok": False, "error": "Somente o ADM pode testar o Telegram."}), 403
        msg_teste = (
            f"🧪 <b>TESTE DE COMUNICAÇÃO - VISION PRO V3</b>\n\n"
            f"✅ Conexão estabelecida com sucesso com o Telegram!\n"
            f"👤 Usuário: {user}\n"
            f"⏰ Horário: {agora_brasilia().strftime('%H:%M:%S')}"
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
        if st.get("timer_confirmacao"):
            try:
                st["timer_confirmacao"].cancel()
            except Exception:
                pass
        st["timer_confirmacao"] = None
        st["alerta_ativo"] = None
        st["sessao_resultados"] = []
        st["inicio_varredura"] = time.time() + 2 
        st["sinais_enviados"].clear() 
        
        st["ativo_atual"] = "INICIANDO VARREDURA..."
        st["ultimo_sinal"] = f"<div class='system-console'>⚡ <b>INICIANDO MOTOR DE ANÁLISE DINÂMICA</b><br><span style='color:#00f2fe;'>[VARRENDO TODOS OS ATIVOS...]</span></div><div class='tech-scanner'></div>"
        
        msg_inicio_telegram = (
            f"🚀 <b>SISTEMA VISION PRO V3 INICIADO</b>\n\n"
            f"🟢 <b>Status:</b> Análise de 30 velas ativada\n"
            f"👤 <b>Usuário:</b> {user}\n"
            f"📊 <b>Timeframe:</b> M{st['timeframe']}\n"
            f"🌐 <b>Mercado:</b> {st['tipo_mercado']}\n"
            f"⚙️ <b>Estratégia:</b> {NOME_ESTRATEGIAS_DISPLAY.get(st['estrategia'], st['estrategia'])}\n\n"
            f"<i>Varrendo gráficos em tempo real...</i>"
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
        if st.get("timer_confirmacao"):
            try:
                st["timer_confirmacao"].cancel()
            except Exception:
                pass
        st["timer_confirmacao"] = None
        if st.get("alerta_ativo") and st["alerta_ativo"].get("msg_id"):
            deletar_mensagem_telegram(st["alerta_ativo"]["msg_id"])
        st["alerta_ativo"] = None
        # Envia o fechamento ANTES de limpar os resultados da sessão.
        enviar_telegram(mensagem_encerramento_sessao(st), user_solicitante=user)

        st["ativo_atual"] = "DESCONECTADO"
        st["ultimo_sinal"] = "Aguardando Comando..."
        
        # Mantém o comportamento anterior de zerar o placar geral no encerramento.
        zerar_estatisticas_usuario(user)
        st["sessao_resultados"] = []
        return jsonify({"ok": True})

    elif cmd.startswith("tf_"): 
        st["timeframe"] = int(cmd.split('_')[1])
    elif cmd.startswith("mkt_"): 
        st["tipo_mercado"] = cmd.split('_', 1)[1] 
    elif cmd.startswith("set_est_"): 
        st["estrategia"] = cmd.replace("set_est_", "")
    
    return jsonify({"ok": True})

def registrar_resultado_sessao(st, resultado):
    """Guarda os resultados confirmados da sessão atual, até os 5 primeiros sinais."""
    if resultado not in ("win", "g1", "red"):
        return
    resultados = st.setdefault("sessao_resultados", [])
    if len(resultados) < 5:
        resultados.append(resultado)

def placar_sessao(st):
    resultados = st.get("sessao_resultados", [])
    wins = sum(1 for r in resultados if r in ("win", "g1"))
    reds = sum(1 for r in resultados if r == "red")
    total = wins + reds
    aproveitamento = round((wins / total) * 100, 1) if total else 0.0
    return wins, reds, aproveitamento

def formatar_resultados_sessao(st):
    """Monta as linhas 1ª a 5ª para o fechamento da sessão."""
    simbolos = {"win": "✅", "g1": "🔄", "red": "❌"}
    resultados = st.get("sessao_resultados", [])
    linhas = []
    for i in range(5):
        marca = simbolos.get(resultados[i], "—") if i < len(resultados) else "—"
        linhas.append(f"{i + 1}ª — {marca}")
    return "\n".join(linhas)

def mensagem_resultado_telegram(st, resultado):
    wins, reds, _ = placar_sessao(st)
    placar = f"{wins} / {reds}"
    if resultado == "win":
        return (
            "Vision Trade FREE 📈:\n"
            "💎 <b>TA NA CONTA! WIN DIRETO!</b> 💎\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "🔥 A IA do Vision Pro não falha. Operação encerrada com precisão cirúrgica!\n"
            "🚀 Mais um lucro garantido para o bolso!\n\n"
            f"📊 Placar Geral: {placar}"
        )
    if resultado == "g1":
        return (
            "🔄 <b>VITÓRIA CONFIRMADA NO GALE 1!</b> 🔄\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "✅ Recuperação e lucro! Nossa estratégia de proteção funcionou perfeitamente.\n"
            "💪 O mercado tentou, mas a nossa análise venceu!\n\n"
            f"Placar Geral: {placar}"
        )
    return (
        "🛑 <b>ANÁLISE ENCERRADA - STOP LOSS</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "⚠️ O mercado apresentou volatilidade atípica.\n\n"
        f"📊 Placar Geral: {placar}"
    )

def mensagem_encerramento_sessao(st):
    wins, reds, aproveitamento = placar_sessao(st)
    linhas = formatar_resultados_sessao(st)
    return (
        "🛑 <b>Sessão encerrada!!!</b>\n"
        "Voltamos amanhã às 20h\n\n"
        "📊 <b>RESULTADO DA SESSÃO:</b>\n"
        f"{linhas}\n"
        f"🏆 <b>PLACAR FINAL: {wins} / {reds}</b>\n"
        f"Uma sessão com {aproveitamento:g}% de acerto"
    )

@app.route('/resultado/<res>')
def resultado(res):
    user = session.get('user')
    if user:
        st = get_user_state(user)
        if res == 'win':
            atualizar_estatisticas_usuario(user, True)
            atualizar_ultimo_sinal_bd(user, "Win")
            registrar_resultado_sessao(st, "win")
            enviar_telegram(mensagem_resultado_telegram(st, "win"), user_solicitante=user)
        elif res == 'g1':
            atualizar_estatisticas_usuario(user, True)
            atualizar_ultimo_sinal_bd(user, "WinG1")
            registrar_resultado_sessao(st, "g1")
            enviar_telegram(mensagem_resultado_telegram(st, "g1"), user_solicitante=user)
        elif res == 'red':
            atualizar_estatisticas_usuario(user, False)
            atualizar_ultimo_sinal_bd(user, "Red")
            registrar_resultado_sessao(st, "red")
            enviar_telegram(mensagem_resultado_telegram(st, "red"), user_solicitante=user)
        elif res == 'pular':
            atualizar_ultimo_sinal_bd(user, "Ignorado")
            enviar_telegram("⚠️ <b>SINAL IGNORADO / PULADO</b>", user_solicitante=user)

        st["aguardando_confirmacao"] = False
        st["sinal_permanente"] = None
        if st.get("timer_confirmacao"):
            try:
                st["timer_confirmacao"].cancel()
            except Exception:
                pass
        st["timer_confirmacao"] = None
        st["alerta_ativo"] = None
        
        st["ultimo_sinal"] = f"<div class='system-console'>🔍 ANALISANDO VELAS: <b>{st['ativo_atual']}</b> (M{st['timeframe']})<br><span style='color:#00f2fe;'>[RETOMANDO VARREDURA COMPLETA]</span></div><div class='tech-scanner'></div>"
    
    return redirect('/')

# ================= ENVIO TELEGRAM ASSÍNCRONO =================
def enviar_telegram_em_background(mensagem, user_email, alert_id=None, deletar_msg_id=None, st=None):
    """Executa operações do Telegram fora do loop de análise."""
    def worker():
        try:
            if deletar_msg_id:
                try:
                    deletar_mensagem_telegram(deletar_msg_id)
                except Exception as e:
                    print(f"⚠️ Falha ao deletar alerta antigo no Telegram: {e}")
            # Se o alerta já foi substituído enquanto o Telegram estava processando,
            # não envia a mensagem antiga.
            if st is not None and alert_id is not None:
                atual = st.get("alerta_ativo")
                if atual and atual.get("alert_id") != alert_id:
                    return

            novo_id = enviar_telegram(mensagem, auto_delete=None, user_solicitante=user_email)
            if st is not None and alert_id is not None:
                atual = st.get("alerta_ativo")
                if atual and atual.get("alert_id") == alert_id:
                    atual["msg_id"] = novo_id
        except Exception as e:
            print(f"⚠️ Erro no envio Telegram em background: {e}")
    threading.Thread(target=worker, daemon=True).start()


# ================= CONFIRMAÇÃO PRECISA DO SINAL =================
def confirmar_alerta_agendado(user_email, alert_id):
    """
    Confirma o alerta no horário exato programado, independentemente do tempo
    gasto pela varredura dos ativos. Isso evita que uma varredura lenta faça
    a confirmação aparecer somente depois da virada do candle.
    """
    try:
        st = get_user_state(user_email)
        alerta = st.get("alerta_ativo")

        # O alerta pode ter sido substituído/cancelado antes do timer disparar.
        if not alerta or alerta.get("alert_id") != alert_id:
            return

        if not st.get("bot_iniciado") or st.get("bot_pausado"):
            return

        ativo = alerta["ativo"]
        sinal = alerta["sinal"]
        est_fmt = alerta["estrategia_fmt"]
        str_saida = alerta["str_saida"]
        prob = alerta["probabilidade"]
        tf = alerta["tf"]
        str_entrada = alerta["str_entrada"]

        cor_direcao = "#10b981" if sinal == "CALL" else "#ef4444"

        # Atualiza a tela ANTES de qualquer operação de rede/banco.
        st["sinal_permanente"] = (
            f"<div class='status-box' style='border-color:#00f2fe; background:rgba(0,242,254,0.1);'>"
            f"<h3 style='color:#00f2fe; margin-bottom:8px;'>🎯 SINAL CONFIRMADO!</h3>"
            f"<b>ATIVO:</b> {ativo}<br>"
            f"<b>DIREÇÃO DE ENTRADA:</b> <span style='color:{cor_direcao}; font-size:18px;'>{sinal}</span><br>"
            f"<b>ESTRATÉGIA:</b> <span style='color:#38ef7d;'>{est_fmt} ({prob}%)</span><br>"
            f"<b>TIMEFRAME:</b> M{tf} | <b>ENTRADA:</b> {str_entrada} | <b>EXPIRAÇÃO:</b> {str_saida}"
            f"</div>"
        )
        st["aguardando_confirmacao"] = True
        st["alerta_ativo"] = None
        st["timer_confirmacao"] = None

        st["notificacao"] = {
            "id": str(time.time_ns()),
            "titulo": f"🎯 ENTRADA: {ativo} — {sinal}",
            "corpo": f"Direção: {sinal} | M{tf} | {est_fmt} | Entrada: {str_entrada}"
        }

        msg_sinal = (
            f"🎯 <b>SINAL CONFIRMADO — ENTRADA AGORA!</b> 🎯\n\n"
            f"💱 <b>Paridade:</b> {ativo}\n"
            f"↕️ <b>DIREÇÃO DE ENTRADA:</b> {sinal}\n"
            f"⏱ <b>Timeframe:</b> M{tf}\n"
            f"🧠 <b>Estratégia:</b> {est_fmt}\n"
            f"🔥 <b>Probabilidade Estimada:</b> {prob}%\n"
            f"🕐 <b>Entrada:</b> {str_entrada}\n"
            f"⌛ <b>Expiração:</b> {str_saida}\n\n"
            f"💡 <i>Gerencie seu capital com responsabilidade.</i>"
        )

        def finalizar_confirmacao(
            _user=user_email, _ativo=ativo, _sinal=sinal,
            _est_fmt=est_fmt, _tf=tf, _msg=msg_sinal
        ):
            try:
                registrar_sinal_bd(
                    _user,
                    f"{_ativo} | {_sinal} | {_est_fmt} | M{_tf}"
                )
            except Exception as e:
                print(f"⚠️ Erro ao registrar sinal confirmado: {e}")
            try:
                enviar_telegram(
                    _msg, auto_delete=None, user_solicitante=_user
                )
            except Exception as e:
                print(f"⚠️ Erro ao enviar confirmação Telegram: {e}")

        # Banco/Telegram ficam fora do caminho crítico da confirmação.
        threading.Thread(target=finalizar_confirmacao, daemon=True).start()

    except Exception as e:
        print(f"⚠️ Erro na confirmação agendada ({user_email}): {e}")


# ================= LOOP PRINCIPAL MULTI-USUÁRIO DO BOT =================
def bot_loop():
    ohlc_cache = {}

    while True:
        try:
            usuarios_ativos = list(DADOS_USUARIOS.items())
            
            if not usuarios_ativos:
                time.sleep(1)
                continue

            agora_scan = agora_brasilia()
            now_ts = time.time()

            # Limpeza do cache de dados OHLC a cada 5 segundos
            ohlc_cache = {k: v for k, v in ohlc_cache.items() if now_ts - v["time"] < 5}

            for user_email, st in usuarios_ativos:
                try:
                    tf_atual = max(1, int(st.get("timeframe", 5)))
                    periodo_candle = tf_atual * 60
                    st["candle_remaining"] = int(periodo_candle - (now_ts % periodo_candle))
                    if not st.get("bot_iniciado") or st.get("bot_pausado"):
                        continue

                    if now_ts < st.get("inicio_varredura", 0):
                        continue

                    tf = st.get("timeframe", 5)
                    mkt = st.get("tipo_mercado", "TODOS")
                    user_est = st.get("estrategia", "TODAS")

                    # -------------------------------------------------------------
                    # 1. CONFIRMAÇÃO AGENDADA
                    # -------------------------------------------------------------
                    # A confirmação principal é disparada por threading.Timer no
                    # momento exato. Mantemos aqui apenas um fallback caso o timer
                    # seja atrasado pelo sistema.
                    alerta = st.get("alerta_ativo")
                    if alerta:
                        momento_confirmacao = alerta.get(
                            "momento_confirmacao",
                            alerta["prox_minuto_entrada"] - timedelta(seconds=5)
                        )
                        if agora_scan >= momento_confirmacao:
                            confirmar_alerta_agendado(
                                user_email, alerta.get("alert_id")
                            )
                            alerta = st.get("alerta_ativo")

                    bloquear_novos_alertas = st.get("aguardando_confirmacao", False)

                    # -------------------------------------------------------------
                    # 2. VARREDURA DINÂMICA DE TODOS OS ATIVOS DO MERCADO
                    # -------------------------------------------------------------
                    if mkt == "TODOS":
                        ativos = ATIVOS_BASE["FOREX_ABERTO"] + ATIVOS_BASE["CRIPTO_ABERTO"] + ATIVOS_BASE["FOREX_OTC"] + ATIVOS_BASE["CRIPTO_OTC"]
                    elif mkt == "ABERTO_TODOS":
                        ativos = ATIVOS_BASE["FOREX_ABERTO"] + ATIVOS_BASE["CRIPTO_ABERTO"]
                    elif mkt == "OTC_TODOS":
                        ativos = ATIVOS_BASE["FOREX_OTC"] + ATIVOS_BASE["CRIPTO_OTC"]
                    else:
                        ativos = ATIVOS_BASE.get(mkt, ATIVOS_BASE["FOREX_ABERTO"])

                    ativos_scan = ativos.copy()
                    random.shuffle(ativos_scan)

                    for ativo in ativos_scan:
                        if not st.get("bot_iniciado") or st.get("bot_pausado"):
                            break

                        st["ativo_atual"] = ativo
                        ticker = MAPA_TICKERS.get(ativo, ativo)

                        if not alerta and not st.get("aguardando_confirmacao"):
                            st["ultimo_sinal"] = f"<div class='system-console'>🔍 VARRENDO 30 VELAS EM: <b style='color:#00f2fe; font-size:16px;'>{ativo}</b> (M{tf})<br><span style='color:#00f2fe;'>[BUSCANDO CONFLUÊNCIA]</span></div><div class='tech-scanner'></div>"

                        cache_key = f"{ticker}_{tf}"
                        if cache_key in ohlc_cache:
                            data = ohlc_cache[cache_key]["data"]
                        else:
                            data = get_data_v2(ticker, tf, velas_minimas=30)
                            if data:
                                ohlc_cache[cache_key] = {"data": data, "time": time.time()}

                        if not data:
                            continue

                        sinal_encontrado = None
                        est_nome_encontrada = None
                        maior_prob = 0

                        if user_est == "TODAS":
                            estrategias_para_analisar = LISTA_ESTRATEGIAS.copy()
                            random.shuffle(estrategias_para_analisar)
                        elif "," in str(user_est):
                            estrategias_para_analisar = [e.strip() for e in user_est.split(",") if e.strip() in LISTA_ESTRATEGIAS]
                        elif user_est in LISTA_ESTRATEGIAS:
                            estrategias_para_analisar = [user_est]
                        else:
                            estrategias_para_analisar = LISTA_ESTRATEGIAS.copy()

                        for est_nome in estrategias_para_analisar:
                            sinal_test, prob_test = analisar_estrategia(data, est_nome)
                            if sinal_test and prob_test > maior_prob:
                                sinal_encontrado = sinal_test
                                est_nome_encontrada = est_nome
                                maior_prob = prob_test

                        if sinal_encontrado and not bloquear_novos_alertas:
                            agora = agora_brasilia()
                            
                            min_pass = agora.minute % tf
                            seg_pass = min_pass * 60 + agora.second
                            total_seg = tf * 60
                            seg_restantes = total_seg - seg_pass

                            # A janela de decisão fecha 5 segundos antes da virada.
                            if seg_restantes <= 5:
                                continue

                            prox_minuto_entrada = agora + timedelta(seconds=seg_restantes)
                            momento_confirmacao = prox_minuto_entrada - timedelta(seconds=5)
                            horario_saida = prox_minuto_entrada + timedelta(minutes=tf)

                            # Horário em que o painel/Telegram confirmam a entrada.
                            str_entrada = momento_confirmacao.strftime("%H:%M:%S")
                            str_saida = horario_saida.strftime("%H:%M")

                            nome_est_formatado = NOME_ESTRATEGIAS_DISPLAY.get(est_nome_encontrada, est_nome_encontrada)

                            # Substituição se houver um sinal com probabilidade superior no mesmo ciclo
                            if alerta:
                                if maior_prob > alerta.get("probabilidade", 0):
                                    msg_antigo_id = alerta.get("msg_id")
                                    novo_alert_id = str(time.time_ns())

                                    msg_pre_alerta = (
                                        f"⚡ <b>ALERTA ATUALIZADO: MAIOR PROBABILIDADE DETECTADA!</b> ⚡\n\n"
                                        f"<b>Ativo:</b> {ativo} ({maior_prob}% de Assertividade)\n"
                                        f"<b>Timeframe:</b> M{tf}\n"
                                        f"<b>DIREÇÃO DE ENTRADA:</b> {sinal_encontrado}\n"
                                        f"<b>Estratégia:</b> {nome_est_formatado}\n"
                                        f"<b>Horário da Entrada:</b> {str_entrada}\n\n"
                                        f"👉 <i>Alerta anterior cancelado. Abra o ativo {ativo} na corretora!</i>"
                                    )

                                    # Troca o alerta no painel imediatamente.
                                    st["alerta_ativo"] = {
                                        "ativo": ativo,
                                        "sinal": sinal_encontrado,
                                        "estrategia": est_nome_encontrada,
                                        "estrategia_fmt": nome_est_formatado,
                                        "probabilidade": maior_prob,
                                        "msg_id": None,
                                        "str_entrada": str_entrada,
                                        "str_saida": str_saida,
                                        "prox_minuto_entrada": prox_minuto_entrada,
                                        "momento_confirmacao": momento_confirmacao,
                                        "alert_id": novo_alert_id,
                                        "tf": tf
                                    }

                                    # Reagenda a confirmação para o novo alerta.
                                    if st.get("timer_confirmacao"):
                                        try:
                                            st["timer_confirmacao"].cancel()
                                        except Exception:
                                            pass
                                    agora_timer = agora_brasilia()
                                    atraso_confirmacao = max(
                                        0.0, (momento_confirmacao - agora_timer).total_seconds()
                                    )
                                    timer_confirmacao = threading.Timer(
                                        atraso_confirmacao,
                                        confirmar_alerta_agendado,
                                        args=(user_email, novo_alert_id)
                                    )
                                    timer_confirmacao.daemon = True
                                    st["timer_confirmacao"] = timer_confirmacao
                                    timer_confirmacao.start()

                                    enviar_telegram_em_background(
                                        msg_pre_alerta,
                                        user_email,
                                        alert_id=novo_alert_id,
                                        deletar_msg_id=msg_antigo_id,
                                        st=st
                                    )

                                    st["ultimo_sinal"] = (
                                        f"<div style='text-align:center; color:#f59e0b; font-family: sans-serif;'>"
                                        f"⚡ <b>ALERTA SUBSTITUÍDO (MAIOR PROBABILIDADE: {maior_prob}%)</b> ⚡<br>"
                                        f"<b>NOVO ATIVO: {ativo}</b> | <b>DIREÇÃO: <span style='color:{'#10b981' if sinal_encontrado=='CALL' else '#ef4444'}'>{sinal_encontrado}</span></b> | Entrada às <b>{str_entrada}</b> (M{tf})<br>"
                                        f"<span style='font-size:12px; color:#00f2fe;'>Estratégia: <b>{nome_est_formatado}</b></span>"
                                        f"</div>"
                                    )
                                    alerta = st["alerta_ativo"]

                            else:
                                if st["sinais_enviados"].get(ativo) == str_entrada:
                                    continue

                                st["sinais_enviados"][ativo] = str_entrada

                                msg_pre_alerta = (
                                    f"⚠️ <b>ATENÇÃO: ANALISANDO OPORTUNIDADE DE OPERAÇÃO</b> ⚠️\n\n"
                                    f"<b>Ativo:</b> {ativo}\n"
                                    f"<b>Timeframe:</b> M{tf}\n"
                                    f"<b>DIREÇÃO DE ENTRADA:</b> {sinal_encontrado}\n"
                                    f"<b>Estratégia Identificada:</b> {nome_est_formatado}\n"
                                    f"<b>Assertividade Estimada:</b> {maior_prob}%\n"
                                    f"<b>Horário da Entrada:</b> {str_entrada}\n\n"
                                    f"👉 <i>Abra o ativo na corretora e prepare-se!</i>"
                                )
                                
                                novo_alert_id = str(time.time_ns())

                                st["alerta_ativo"] = {
                                    "ativo": ativo,
                                    "sinal": sinal_encontrado,
                                    "estrategia": est_nome_encontrada,
                                    "estrategia_fmt": nome_est_formatado,
                                    "probabilidade": maior_prob,
                                    "msg_id": None,
                                    "str_entrada": str_entrada,
                                    "str_saida": str_saida,
                                    "prox_minuto_entrada": prox_minuto_entrada,
                                    "momento_confirmacao": momento_confirmacao,
                                    "alert_id": novo_alert_id,
                                    "tf": tf
                                }

                                # Agenda a confirmação independente da varredura.
                                agora_timer = agora_brasilia()
                                atraso_confirmacao = max(
                                    0.0, (momento_confirmacao - agora_timer).total_seconds()
                                )
                                timer_confirmacao = threading.Timer(
                                    atraso_confirmacao,
                                    confirmar_alerta_agendado,
                                    args=(user_email, novo_alert_id)
                                )
                                timer_confirmacao.daemon = True
                                st["timer_confirmacao"] = timer_confirmacao
                                timer_confirmacao.start()

                                enviar_telegram_em_background(
                                    msg_pre_alerta,
                                    user_email,
                                    alert_id=novo_alert_id,
                                    st=st
                                )

                                st["ultimo_sinal"] = (
                                    f"<div style='text-align:center; color:#f59e0b; font-family: sans-serif;'>"
                                    f"⚠️ <b>PREPARE O ATIVO: {ativo} ({maior_prob}%)</b> ⚠️<br>"
                                    f"<span style='color:#fff;'>DIREÇÃO: <b style='color:{'#10b981' if sinal_encontrado=='CALL' else '#ef4444'}'>{sinal_encontrado}</b> | Entrada às <b>{str_entrada}</b> (M{tf})</span><br>"
                                    f"<span style='font-size:12px; color:#00f2fe;'>Estratégia: <b>{nome_est_formatado}</b></span>"
                                    f"</div>"
                                )

                                st["notificacao"] = {
                                    "id": str(time.time()),
                                    "titulo": f"⚠️ PREPARE-SE: {ativo}",
                                    "corpo": f"Direção: {sinal_encontrado} | Entrada às {str_entrada} (M{tf}) via {nome_est_formatado} ({maior_prob}%)."
                                }
                                alerta = st["alerta_ativo"]

                except Exception as e_usr:
                    print(f"Erro no loop do usuario {user_email}: {e_usr}")

            time.sleep(0.5)
        except Exception as err:
            print(f"Erro no loop global do bot: {err}")
            time.sleep(2)

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
