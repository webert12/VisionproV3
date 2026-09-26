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
import html as html_lib
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
            "telegram_alert_status": {},  # status dos alertas Telegram: active/cancelled
            "ultima_confirmacao_msg_id": None,  # última confirmação enviada ao Telegram
            "ultima_confirmacao_alert_id": None,
            "notificacao": None,
            "notificacao_ultima_hora": 0.0,
            "candle_remaining": 0,
            "news_guard_status": "AGUARDANDO CALENDÁRIO",
            "news_guard_event": None,
            "news_blocked_assets": [],
            "news_guard_updated": 0.0,
            "analise_atual": None,
            "sessao_resultados": [],
            "sinais_sessao_total": 0
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
    <meta name="viewport" content="width=device-width, initial-scale=1.0, viewport-fit=cover">
    <meta name="theme-color" content="#070b12">
    <title>VISION PRO V4 — Terminal de Análise</title>
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800;900&family=JetBrains+Mono:wght@500;700&display=swap" rel="stylesheet">
    <style>
        :root {
            --bg:#070b12; --panel:#0b111b; --panel2:#0f1724; --line:#1d2938;
            --text:#e8eef6; --muted:#8290a3; --cyan:#00d9ff; --green:#22c55e;
            --red:#ef4444; --amber:#f59e0b; --blue:#60a5fa; --purple:#a78bfa;
            --shadow:0 18px 55px rgba(0,0,0,.35);
        }
        *{box-sizing:border-box;margin:0;padding:0;font-family:Inter,sans-serif}
        html,body{min-height:100%;background:var(--bg);color:var(--text)}
        body{overflow-x:hidden}
        button,select{font:inherit}
        button{cursor:pointer}
        .app-shell{min-height:100vh;display:flex}
        .sidebar{position:fixed;inset:0 auto 0 0;width:238px;background:rgba(8,13,21,.96);border-right:1px solid var(--line);padding:20px 14px;display:flex;flex-direction:column;z-index:50}
        .logo{display:flex;align-items:center;gap:10px;padding:4px 8px 22px;border-bottom:1px solid var(--line)}
        .logo-mark{width:38px;height:38px;border-radius:12px;display:grid;place-items:center;background:linear-gradient(145deg,#0b2530,#0b1520);border:1px solid rgba(0,217,255,.4);color:var(--cyan);font-weight:900;box-shadow:0 0 25px rgba(0,217,255,.08)}
        .logo-title{font-size:15px;font-weight:900;letter-spacing:1.2px}.logo-sub{font-size:9px;color:var(--muted);margin-top:2px;letter-spacing:.7px}
        .nav{padding-top:18px;display:grid;gap:6px}.nav button{width:100%;border:1px solid transparent;background:transparent;color:#91a0b4;text-align:left;padding:11px 12px;border-radius:10px;font-size:11px;font-weight:800;display:flex;align-items:center;gap:10px;transition:.18s}.nav button:hover,.nav button.active{background:rgba(0,217,255,.07);border-color:rgba(0,217,255,.18);color:#eafcff}.nav button.active{box-shadow:inset 3px 0 0 var(--cyan)}
        .sidebar-footer{margin-top:auto;color:#586679;font-size:9px;line-height:1.6;padding:12px 8px}
        .main{width:calc(100% - 238px);margin-left:238px;min-height:100vh;padding:20px 24px 90px}
        .topbar{display:flex;align-items:center;justify-content:space-between;gap:15px;max-width:1500px;margin:0 auto 16px}
        .page-title{font-size:20px;font-weight:900;letter-spacing:.2px}.page-title span{color:var(--cyan)}
        .top-meta{display:flex;align-items:center;gap:8px}.status-pill{padding:7px 10px;border:1px solid rgba(34,197,94,.25);background:rgba(34,197,94,.07);border-radius:999px;color:#86efac;font-size:10px;font-weight:900}.logout{padding:7px 11px;border-radius:9px;text-decoration:none;color:#fca5a5;border:1px solid rgba(239,68,68,.22);background:rgba(239,68,68,.06);font-size:10px;font-weight:800}
        .workspace{max-width:1500px;margin:auto}.view{display:none}.view.active{display:block}
        .grid-main{display:grid;grid-template-columns:minmax(0,1.55fr) minmax(300px,.85fr);gap:14px;align-items:start}
        .card{background:linear-gradient(145deg,rgba(15,23,36,.98),rgba(9,15,24,.98));border:1px solid var(--line);border-radius:16px;box-shadow:var(--shadow);overflow:hidden}.card-pad{padding:16px}.card-head{display:flex;justify-content:space-between;align-items:center;gap:10px;padding:14px 16px;border-bottom:1px solid var(--line)}.eyebrow{font-size:9px;color:#718096;font-weight:900;letter-spacing:1.1px;text-transform:uppercase}.card-title{font-size:13px;font-weight:900;margin-top:4px}.mini{font-size:10px;color:var(--muted)}
        .hero{min-height:300px;position:relative;background:radial-gradient(circle at 50% 0%,rgba(0,217,255,.07),transparent 50%),linear-gradient(145deg,#0d1724,#090e17);border-color:rgba(0,217,255,.18)}
        .hero-top{display:flex;justify-content:space-between;gap:10px;align-items:flex-start}.asset-tag{font-family:'JetBrains Mono';font-size:12px;color:#d8f9ff;background:rgba(0,217,255,.08);border:1px solid rgba(0,217,255,.18);padding:7px 9px;border-radius:9px}.live-dot{display:inline-block;width:7px;height:7px;border-radius:50%;background:var(--green);box-shadow:0 0 10px rgba(34,197,94,.8);margin-right:5px}
        .signal-center{text-align:center;padding:18px 8px 12px}.signal-direction{font-size:40px;font-weight:900;letter-spacing:1px}.signal-call{color:#34d399;text-shadow:0 0 24px rgba(34,197,94,.18)}.signal-put{color:#fb7185;text-shadow:0 0 24px rgba(239,68,68,.18)}.signal-wait{color:#7f8da0;font-size:25px}.prob-label{font-size:10px;color:#7d8ba0;font-weight:800;text-transform:uppercase;letter-spacing:1px}.prob-value{font-family:'JetBrains Mono';font-size:31px;font-weight:900;margin-top:2px}.prob-bar{height:8px;background:#182231;border-radius:99px;overflow:hidden;margin:10px auto 12px;max-width:330px}.prob-fill{height:100%;width:0%;background:linear-gradient(90deg,#0ea5e9,#22c55e);border-radius:99px;transition:width .4s}
        .signal-meta{display:grid;grid-template-columns:repeat(4,1fr);gap:7px}.metric{background:#0b121d;border:1px solid #182536;border-radius:10px;padding:9px;text-align:center}.metric .k{font-size:8px;color:#66758a;text-transform:uppercase;font-weight:900}.metric .v{font-size:11px;font-weight:900;margin-top:4px;color:#dce6f2}
        .console{margin-top:12px;background:#070c13;border:1px solid #182333;border-radius:10px;padding:10px;font-family:'JetBrains Mono';font-size:10px;color:#7de3f5;min-height:34px;line-height:1.5}
        .chart-wrap{padding:10px 12px 12px}.chart{width:100%;height:150px;display:block;background:#080e16;border:1px solid #172333;border-radius:10px}.chart-grid{stroke:#172333;stroke-width:1}.chart-line{fill:none;stroke:#00d9ff;stroke-width:2.2;vector-effect:non-scaling-stroke}.chart-area{fill:url(#areaGrad);opacity:.25}.chart-empty{fill:#657489;font-size:11px}
        .confluence{display:grid;gap:7px}.conf-row{display:grid;grid-template-columns:110px 1fr 42px;align-items:center;gap:8px;font-size:9px}.conf-name{color:#a6b3c4;font-weight:800}.conf-bar{height:7px;background:#172231;border-radius:99px;overflow:hidden}.conf-fill{height:100%;border-radius:99px;background:linear-gradient(90deg,#38bdf8,#22c55e)}.conf-points{text-align:right;color:#d9e5f1;font-family:'JetBrains Mono';font-size:9px}
        .analysis-reasons{display:grid;grid-template-columns:1fr 1fr;gap:7px;margin-top:10px}.reason{padding:9px;border:1px solid #182536;background:#0b121c;border-radius:10px}.reason b{font-size:9px}.reason div{font-size:9px;color:#75859a;margin-top:3px;line-height:1.4}.ok{color:#4ade80}.warn{color:#fbbf24}.bad{color:#fb7185}
        .stat-grid{display:grid;grid-template-columns:repeat(4,1fr);gap:8px}.stat-box{background:#0b121d;border:1px solid #182536;border-radius:12px;padding:11px;text-align:center}.stat-k{font-size:8px;color:#66758a;font-weight:900;text-transform:uppercase}.stat-v{font-family:'JetBrains Mono';font-size:19px;font-weight:900;margin-top:3px}.win{color:#4ade80}.loss{color:#fb7185}.blue{color:#60a5fa}
        .session-bar{height:7px;background:#182231;border-radius:99px;overflow:hidden;margin-top:10px}.session-fill{height:100%;background:linear-gradient(90deg,#16a34a,#4ade80);width:0%;transition:.4s}
        .protection{display:grid;gap:8px}.protection-main{display:flex;justify-content:space-between;gap:8px;align-items:center}.guard-badge{padding:6px 8px;border-radius:8px;background:rgba(34,197,94,.07);border:1px solid rgba(34,197,94,.18);font-size:9px;font-weight:900;color:#86efac}.locked-btn{width:100%;padding:10px;border:1px solid rgba(239,68,68,.25);background:rgba(239,68,68,.06);color:#fca5a5;border-radius:9px;font-size:9px;font-weight:900;text-transform:uppercase}.locked-panel{display:none;border:1px solid rgba(239,68,68,.2);background:#080e15;border-radius:10px;padding:8px}.locked-panel.open{display:block}.locked-item{padding:8px;border-left:3px solid #ef4444;background:rgba(239,68,68,.05);border-radius:7px;margin-top:5px;font-size:9px;line-height:1.5}.locked-item:first-child{margin-top:0}.empty{font-size:9px;color:#66758a;text-align:center;padding:8px}
        .controls{display:grid;gap:12px}.action-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:7px}.action{border:0;border-radius:10px;padding:11px 5px;color:white;font-size:10px;font-weight:900}.start{background:linear-gradient(135deg,#16a34a,#059669)}.pause{background:linear-gradient(135deg,#f59e0b,#d97706)}.stop{background:linear-gradient(135deg,#ef4444,#dc2626)}.action:active{transform:scale(.98)}.field-grid{display:grid;grid-template-columns:1fr 1fr;gap:8px}.field label{display:block;color:#728197;font-size:8px;font-weight:900;text-transform:uppercase;margin-bottom:4px}.select{width:100%;background:#0a111b;border:1px solid #1b2a3b;color:#dce6f2;padding:10px;border-radius:9px;font-size:10px;outline:none}.select:focus{border-color:rgba(0,217,255,.55)}
        .tool-btn,.history-btn,.admin-btn{width:100%;padding:10px;border-radius:9px;background:#0b131f;border:1px solid #1c2b3d;color:#8edff0;font-size:9px;font-weight:900;text-transform:uppercase}.tool-btn:hover,.history-btn:hover{border-color:rgba(0,217,255,.35)}.admin-btn{color:#8ab4ff;border-color:rgba(96,165,250,.25)}.tools-content,.history{display:none;margin-top:8px}.tools-content.open,.history.open{display:grid;gap:7px}.tg-btn,.notify-btn{width:100%;padding:9px;border-radius:8px;background:#0a111a;border:1px solid #1d2b3c;color:#94a3b8;font-size:9px;font-weight:900}.history-list{max-height:220px;overflow:auto}.history-item{display:flex;justify-content:space-between;gap:8px;padding:8px 0;border-bottom:1px solid rgba(255,255,255,.05);font-family:'JetBrains Mono';font-size:9px}.history-item:last-child{border-bottom:0}
        .section{display:none}.section.active{display:block}.section-head{display:flex;justify-content:space-between;align-items:center;margin-bottom:12px}.section-head h2{font-size:15px}.section-head p{font-size:9px;color:#69798d}.table-card{overflow:auto}.data-table{width:100%;border-collapse:collapse;min-width:620px}.data-table th{font-size:8px;color:#66758a;text-transform:uppercase;text-align:left;padding:10px;border-bottom:1px solid var(--line)}.data-table td{font-size:9px;padding:10px;border-bottom:1px solid rgba(255,255,255,.045)}
        .mobile-nav{display:none}.mobile-only{display:none}.desktop-only{display:block}
        .toast{position:fixed;right:20px;bottom:20px;background:#101a28;border:1px solid #24354a;border-radius:10px;padding:10px 13px;font-size:10px;color:#dbe8f5;opacity:0;transform:translateY(10px);pointer-events:none;transition:.2s;z-index:100}.toast.show{opacity:1;transform:none}
        @media(max-width:1100px){.grid-main{grid-template-columns:1fr}.sidebar{width:210px}.main{width:calc(100% - 210px);margin-left:210px}.signal-meta{grid-template-columns:repeat(2,1fr)}}
        @media(max-width:760px){
            body{padding:0;background:#070b12}.app-shell{display:block}.sidebar{display:none}.main{width:100%;margin:0;padding:12px 10px 86px}.topbar{margin-bottom:11px}.page-title{font-size:16px}.status-pill{font-size:8px;padding:6px 8px}.logout{font-size:8px;padding:6px 8px}.mobile-only{display:block}.desktop-only{display:none}
            .grid-main{display:flex;flex-direction:column;gap:10px}.card{border-radius:13px}.card-pad{padding:12px}.hero{min-height:280px}.signal-direction{font-size:35px}.prob-value{font-size:28px}.signal-meta{grid-template-columns:repeat(2,1fr)}.analysis-reasons{grid-template-columns:1fr}.conf-row{grid-template-columns:92px 1fr 36px}.chart{height:135px}.stat-grid{grid-template-columns:repeat(2,1fr)}.field-grid{grid-template-columns:1fr}.action-grid{position:sticky;bottom:72px;z-index:20;background:rgba(7,11,18,.92);padding:7px;border:1px solid #182333;border-radius:12px;backdrop-filter:blur(12px)}
            .mobile-nav{position:fixed;display:grid;grid-template-columns:repeat(5,1fr);left:8px;right:8px;bottom:8px;height:58px;background:rgba(8,14,22,.96);border:1px solid #203044;border-radius:16px;z-index:60;box-shadow:0 10px 35px rgba(0,0,0,.45);padding:4px}.mobile-nav button{border:0;background:transparent;color:#65758a;font-size:8px;font-weight:900;border-radius:11px}.mobile-nav button.active{background:rgba(0,217,255,.08);color:#dffbff}.mobile-nav span{display:block;font-size:17px;margin-bottom:2px}
            .topbar .top-meta{gap:5px}.topbar{gap:6px}.hero-top .mini{max-width:160px}.locked-btn{padding:11px}.section-head{margin-top:2px}.table-card{border-radius:12px}
        }
        @media(min-width:1400px){.main{padding-left:32px;padding-right:32px}.grid-main{grid-template-columns:minmax(0,1.65fr) minmax(350px,.8fr)}}
        @media(prefers-reduced-motion:reduce){*{scroll-behavior:auto!important;transition:none!important;animation:none!important}}
    </style>
</head>
<body>
<div class="app-shell">
    <aside class="sidebar">
        <div class="logo"><div class="logo-mark">VP</div><div><div class="logo-title">VISION PRO</div><div class="logo-sub">V4 ANALYTICS TERMINAL</div></div></div>
        <nav class="nav">
            <button class="active" data-view="central" onclick="abrirView('central',this)">🏠 <span>Central</span></button>
            <button data-view="analise" onclick="abrirView('analise',this)">📊 <span>Análise</span></button>
            <button data-view="sinais" onclick="abrirView('sinais',this)">🎯 <span>Sinais</span></button>
            <button data-view="protecao" onclick="abrirView('protecao',this)">🛡️ <span>Proteção</span></button>
            <button data-view="historico" onclick="abrirView('historico',this)">📈 <span>Histórico</span></button>
            <button data-view="config" onclick="abrirView('config',this)">⚙️ <span>Configurações</span></button>
        </nav>
        <div class="sidebar-footer">Sistema de análise estatística e técnica. Os alertas não garantem resultados financeiros. Opere com responsabilidade.</div>
    </aside>

    <main class="main">
        <div class="topbar">
            <div><div class="page-title">VISION <span>PRO</span></div><div class="mini">Terminal de análise em tempo real</div></div>
            <div class="top-meta"><div class="status-pill"><span class="live-dot"></span><span id="top-status">ONLINE</span></div><a class="logout" href="/logout">SAIR</a></div>
        </div>

        <div class="workspace">
            <section id="view-central" class="view active">
                <div class="grid-main">
                    <div>
                        <div class="card hero">
                            <div class="card-pad">
                                <div class="hero-top">
                                    <div><div class="eyebrow">Sinal operacional</div><div class="card-title">Monitoramento de mercado</div></div>
                                    <div class="asset-tag" id="asset-tag">AGUARDANDO</div>
                                </div>
                                <div class="signal-center">
                                    <div class="signal-direction signal-wait" id="signal-direction">AGUARDANDO</div>
                                    <div class="prob-label">Probabilidade estimada</div>
                                    <div class="prob-value" id="prob-value">--%</div>
                                    <div class="mini" id="confluence-overall">Confluência técnica: --/100</div>
                                    <div class="prob-bar"><div class="prob-fill" id="prob-fill"></div></div>
                                    <div class="mini" id="signal-strategy">Motor aguardando análise</div>
                                </div>
                                <div class="signal-meta">
                                    <div class="metric"><div class="k">Timeframe</div><div class="v" id="signal-tf">M5</div></div>
                                    <div class="metric"><div class="k">Entrada</div><div class="v" id="signal-entry">--:--:--</div></div>
                                    <div class="metric"><div class="k">Expiração</div><div class="v" id="signal-expiry">--:--</div></div>
                                    <div class="metric"><div class="k">Status</div><div class="v" id="signal-status">AGUARDANDO</div></div>
                                </div>
                                <div class="console" id="panel-text">Aguardando Comando...</div>
                            </div>
                            <div class="chart-wrap"><canvas id="market-chart" class="chart"></canvas></div>
                        </div>

                        <div class="card" style="margin-top:12px">
                            <div class="card-head"><div><div class="eyebrow">Confluência técnica</div><div class="card-title">Raio-X do sinal</div></div><div class="mini" id="analysis-direction">Sem sinal</div></div>
                            <div class="card-pad">
                                <div class="confluence" id="confluence-list"><div class="empty">Aguardando dados do mercado...</div></div>
                                <div class="analysis-reasons" id="analysis-reasons"></div>
                            </div>
                        </div>
                    </div>

                    <aside>
                        <div class="card">
                            <div class="card-head"><div><div class="eyebrow">Sessão</div><div class="card-title">Desempenho atual</div></div><div class="mini" id="session-count">0 operações</div></div>
                            <div class="card-pad">
                                <div class="stat-grid">
                                    <div class="stat-box"><div class="stat-k">Wins</div><div class="stat-v win" id="win-count">0</div></div>
                                    <div class="stat-box"><div class="stat-k">Loss</div><div class="stat-v loss" id="loss-count">0</div></div>
                                    <div class="stat-box"><div class="stat-k">Assert.</div><div class="stat-v blue" id="wr-text">0%</div></div>
                                    <div class="stat-box"><div class="stat-k">G1</div><div class="stat-v" id="g1-count">0</div></div>
                                </div>
                                <div class="session-bar"><div class="session-fill" id="wr-fill"></div></div>
                            </div>
                        </div>

                        <div class="card" style="margin-top:12px">
                            <div class="card-head"><div><div class="eyebrow">Proteção</div><div class="card-title">Calendário macro</div></div><div class="guard-badge" id="guard-badge">● ATIVO</div></div>
                            <div class="card-pad protection">
                                <div class="protection-main"><span class="mini">Status da trava</span><b id="news-guard-status" style="font-size:9px;color:#fbbf24">AGUARDANDO CALENDÁRIO</b></div>
                                <button class="locked-btn" id="news-locked-toggle" onclick="toggleAtivosBloqueados()">🔒 VER ATIVOS BLOQUEADOS (0)</button>
                                <div id="news-locked-panel" class="locked-panel"><div id="news-locked-list"><div class="empty">Nenhum ativo bloqueado por notícia no momento.</div></div></div>
                            </div>
                        </div>

                        <div class="card" style="margin-top:12px">
                            <div class="card-head"><div><div class="eyebrow">Controle</div><div class="card-title">Robô</div></div><div class="mini" id="robot-status">PARADO</div></div>
                            <div class="card-pad controls">
                                <div class="action-grid"><button class="action start" onclick="sendCommand('start_bot')">▶ START</button><button class="action pause" onclick="sendCommand('pause_bot')">⏸ PAUSE</button><button class="action stop" onclick="sendCommand('stop_bot')">⏹ STOP</button></div>
                                <button class="tool-btn" onclick="toggleBox('tools-content')">⚙️ FERRAMENTAS E NOTIFICAÇÕES</button>
                                <div id="tools-content" class="tools-content">
                                    <button class="notify-btn" id="btn-enable-notify" onclick="solicitarPermissaoNotificacao()">🔔 ATIVAR NOTIFICAÇÕES NO CELULAR</button>
                                    {% if user == admin %}
                                    <button class="tg-btn" onclick="sendCommand('test_telegram')">🧪 TESTAR TELEGRAM</button>
                                    <button class="tg-btn" id="btn-telegram-toggle" onclick="toggleTelegram()">{{ '🟢 ENVIO TELEGRAM ATIVADO' if telegram_ativo else '🔴 ENVIO TELEGRAM DESATIVADO' }}</button>
                                    <button class="admin-btn" onclick="location.href='/admin_panel'">🛡️ PAINEL ADMINISTRATIVO</button>
                                    {% endif %}
                                </div>
                            </div>
                        </div>
                    </aside>
                </div>
            </section>

            <section id="view-analise" class="view">
                <div class="section-head"><div><h2>📊 Análise detalhada</h2><p>Indicadores e confluências usadas pelo motor.</p></div><div class="asset-tag" id="analysis-asset">AGUARDANDO</div></div>
                <div class="grid-main">
                    <div class="card"><div class="card-head"><div><div class="eyebrow">Mercado</div><div class="card-title">Leitura técnica</div></div></div><div class="chart-wrap"><canvas id="market-chart-2" class="chart"></canvas></div><div class="card-pad"><div class="confluence" id="confluence-list-2"></div></div></div>
                    <div class="card"><div class="card-head"><div><div class="eyebrow">Diagnóstico</div><div class="card-title">Motivos do sinal</div></div></div><div class="card-pad"><div class="analysis-reasons" id="analysis-reasons-2"></div><div style="margin-top:12px" class="metric"><div class="k">Probabilidade estimada</div><div class="v" id="prob-value-2">--%</div></div></div></div>
                </div>
            </section>

            <section id="view-sinais" class="view">
                <div class="section-head"><div><h2>🎯 Sinais</h2><p>O sinal atual fica destacado e sincronizado com o Telegram.</p></div></div>
                <div class="card"><div class="card-pad"><div class="signal-center"><div class="signal-direction signal-wait" id="signal-direction-2">AGUARDANDO</div><div class="prob-label">Probabilidade estimada</div><div class="prob-value" id="prob-value-3">--%</div><div class="prob-bar"><div class="prob-fill" id="prob-fill-3"></div></div><div class="mini" id="signal-strategy-2">--</div></div><div class="signal-meta"><div class="metric"><div class="k">Ativo</div><div class="v" id="signal-asset-2">--</div></div><div class="metric"><div class="k">Timeframe</div><div class="v" id="signal-tf-2">M5</div></div><div class="metric"><div class="k">Entrada</div><div class="v" id="signal-entry-2">--:--:--</div></div><div class="metric"><div class="k">Expiração</div><div class="v" id="signal-expiry-2">--:--</div></div></div></div></div>
                <div id="result-area" class="action-grid" style="display:none;margin-top:12px"><button class="action start" onclick="registrarResultado('win')">WIN</button><button class="action pause" onclick="registrarResultado('g1')">G1</button><button class="action stop" onclick="registrarResultado('red')">RED</button></div>
                <button class="history-btn" style="margin-top:8px" onclick="registrarResultado('pular')">⏭️ PULAR SINAL</button>
            </section>

            <section id="view-protecao" class="view">
                <div class="section-head"><div><h2>🛡️ Proteção macro</h2><p>Eventos de impacto moderado/alto retiram somente os ativos afetados da análise.</p></div></div>
                <div class="card"><div class="card-pad"><div class="protection-main"><div><div class="eyebrow">Calendário econômico</div><div class="card-title" id="guard-detail-status">Aguardando atualização</div></div><div class="guard-badge" id="guard-badge-2">● ATIVO</div></div><div style="margin-top:12px" id="news-locked-list-2"><div class="empty">Nenhum ativo bloqueado por notícia no momento.</div></div></div></div>
            </section>

            <section id="view-historico" class="view">
                <div class="section-head"><div><h2>📈 Histórico</h2><p>Últimos sinais registrados para esta conta.</p></div></div>
                <div class="grid-main" style="margin-bottom:12px"><div class="card"><div class="card-head"><div><div class="eyebrow">Por estratégia</div><div class="card-title">Desempenho recente</div></div></div><div class="card-pad" id="strategy-summary"><div class="empty">Aguardando histórico.</div></div></div><div class="card"><div class="card-head"><div><div class="eyebrow">Por ativo</div><div class="card-title">Desempenho recente</div></div></div><div class="card-pad" id="asset-summary"><div class="empty">Aguardando histórico.</div></div></div></div><div class="card table-card"><table class="data-table"><thead><tr><th>ID</th><th>Sinal</th><th>Resultado</th></tr></thead><tbody id="history-table-body"><tr><td colspan="3" class="empty">Nenhum histórico.</td></tr></tbody></table></div>
            </section>

            <section id="view-config" class="view">
                <div class="section-head"><div><h2>⚙️ Configurações</h2><p>Parâmetros operacionais do motor.</p></div></div>
                <div class="card"><div class="card-pad controls">
                    <div class="field-grid">
                        <div class="field"><label>Mercado</label><select class="select" onchange="sendCommand('mkt_'+this.value)"><option value="TODOS" {% if modo == 'TODOS' %}selected{% endif %}>🌐 Todos</option><option value="ABERTO_TODOS" {% if modo == 'ABERTO_TODOS' %}selected{% endif %}>🟢 Aberto</option><option value="OTC_TODOS" {% if modo == 'OTC_TODOS' %}selected{% endif %}>🌙 OTC</option><option value="FOREX_ABERTO" {% if modo == 'FOREX_ABERTO' %}selected{% endif %}>📈 Forex Aberto</option><option value="CRIPTO_ABERTO" {% if modo == 'CRIPTO_ABERTO' %}selected{% endif %}>🪙 Cripto Aberto</option><option value="FOREX_OTC" {% if modo == 'FOREX_OTC' %}selected{% endif %}>📊 Forex OTC</option><option value="CRIPTO_OTC" {% if modo == 'CRIPTO_OTC' %}selected{% endif %}>⚡ Cripto OTC</option></select></div>
                        <div class="field"><label>Timeframe</label><select class="select" onchange="sendCommand('tf_'+this.value)"><option value="1" {% if tf == 1 %}selected{% endif %}>M1</option><option value="5" {% if tf == 5 %}selected{% endif %}>M5</option><option value="15" {% if tf == 15 %}selected{% endif %}>M15</option></select></div>
                    </div>
                    <div class="field"><label>Estratégia operacional</label><select class="select" onchange="sendCommand('set_est_'+this.value)"><option value="TODAS" {% if estrat == 'TODAS' %}selected{% endif %}>💎 TODAS — análise dinâmica múltipla</option><option value="LOGICA_DO_PRECO" {% if estrat == 'LOGICA_DO_PRECO' %}selected{% endif %}>Lógica do Preço</option><option value="RSI_MACD_MA" {% if estrat == 'RSI_MACD_MA' %}selected{% endif %}>RSI + MACD + MA</option><option value="MHI1" {% if estrat == 'MHI1' %}selected{% endif %}>MHI 1 + Tendência</option><option value="REVERSAO" {% if estrat == 'REVERSAO' %}selected{% endif %}>Reversão de Bandas</option></select></div>
                    <div class="metric"><div class="k">Regra de proteção</div><div class="v">🐂🐂 / 🐂🐂🐂 → trava ±30 min</div></div>
                    <div class="metric"><div class="k">Dados</div><div class="v">Somente candles válidos e fechados</div></div>
                </div></div>
            </section>
        </div>
    </main>
</div>

<div class="mobile-nav">
    <button class="active" data-view="central" onclick="abrirView('central',this)"><span>🏠</span>Início</button>
    <button data-view="analise" onclick="abrirView('analise',this)"><span>📊</span>Análise</button>
    <button data-view="sinais" onclick="abrirView('sinais',this)"><span>🎯</span>Sinais</button>
    <button data-view="protecao" onclick="abrirView('protecao',this)"><span>🛡️</span>Proteção</button>
    <button data-view="config" onclick="abrirView('config',this)"><span>⚙️</span>Config</button>
</div>
<div id="toast" class="toast"></div>

<script>
let lastNotifId=null;
let latestData=null;
const NATIVE_NOTIFICATION_COOLDOWN_MS=0;

function abrirView(name,btn){
    document.querySelectorAll('.view').forEach(v=>v.classList.remove('active'));
    const el=document.getElementById('view-'+name); if(el) el.classList.add('active');
    document.querySelectorAll('[data-view]').forEach(b=>b.classList.toggle('active',b.dataset.view===name));
    window.scrollTo({top:0,behavior:'smooth'});
    if(name==='analise' && latestData) renderAnalysis(latestData);
    if(name==='historico' && latestData) renderHistory(latestData.historico||[]);
}
function toggleBox(id){const e=document.getElementById(id); if(e)e.classList.toggle('open')}
function toast(msg){const e=document.getElementById('toast');if(!e)return;e.innerText=msg;e.classList.add('show');setTimeout(()=>e.classList.remove('show'),2200)}
function sendCommand(cmd){fetch('/command/'+cmd,{cache:'no-store'}).then(r=>r.json()).then(d=>{if(d.redirect)location.href=d.redirect;else if(d.error)toast(d.error);else toast('Comando atualizado');}).catch(()=>toast('Falha de comunicação com o servidor'))}
function registrarResultado(res){fetch('/resultado/'+res,{cache:'no-store'}).then(()=>toast('Resultado registrado')).catch(()=>toast('Falha ao registrar resultado'))}
function toggleTelegram(){fetch('/command/telegram_toggle',{cache:'no-store'}).then(r=>r.json()).then(d=>{if(d.ok){const b=document.getElementById('btn-telegram-toggle');if(b)b.innerText=d.telegram_ativo?'🟢 ENVIO TELEGRAM ATIVADO':'🔴 ENVIO TELEGRAM DESATIVADO';}})}
function solicitarPermissaoNotificacao(){if(!('Notification'in window)){alert('Este navegador não suporta notificações.');return}Notification.requestPermission().then(p=>{const b=document.getElementById('btn-enable-notify');if(p==='granted'){if(b)b.innerText='✅ NOTIFICAÇÕES NATIVAS ATIVADAS';toast('Notificações ativadas')}else alert('Permissão de notificação recusada.')})}
if('serviceWorker'in navigator&&'Notification'in window){navigator.serviceWorker.register('/sw.js',{updateViaCache:'none'}).catch(()=>{})}
async function dispararNotificacaoNativa(titulo,corpo,id){if(!('Notification'in window)||Notification.permission!=='granted')return;const nid=String(id||''),now=Date.now(),last=localStorage.getItem('vision_last_notif_id')||'',lastAt=Number(localStorage.getItem('vision_last_notif_at')||0);if(nid&&nid===last)return;if(lastAt&&NATIVE_NOTIFICATION_COOLDOWN_MS>0&&now-lastAt<NATIVE_NOTIFICATION_COOLDOWN_MS)return;try{const opcoes={body:corpo,tag:nid?'vision-signal-'+nid:'vision-signal-'+now,renotify:true,requireInteraction:true,silent:false,vibrate:[250,120,250,120,400],timestamp:now};if('serviceWorker'in navigator){const reg=await navigator.serviceWorker.ready;await reg.showNotification(titulo,opcoes)}else new Notification(titulo,opcoes);if(nid)localStorage.setItem('vision_last_notif_id',nid);localStorage.setItem('vision_last_notif_at',String(now))}catch(e){console.warn('Notificação nativa indisponível:',e)}}
function toggleAtivosBloqueados(){const p=document.getElementById('news-locked-panel');const b=document.getElementById('news-locked-toggle');if(!p||!b)return;p.classList.toggle('open');const n=(latestData&&latestData.news_blocked_assets||[]).length;b.innerText=(p.classList.contains('open')?'🔽 OCULTAR':'🔒 VER')+' ATIVOS BLOQUEADOS ('+n+')'}
function atualizarAtivosBloqueados(lista){const itens=Array.isArray(lista)?lista:[];const b=document.getElementById('news-locked-toggle'),p=document.getElementById('news-locked-panel'),box=document.getElementById('news-locked-list');if(!b||!p||!box)return;b.innerText=(p.classList.contains('open')?'🔽 OCULTAR':'🔒 VER')+' ATIVOS BLOQUEADOS ('+itens.length+')';box.innerHTML=itens.length?itens.map(x=>{const imp=Math.max(1,Math.min(3,parseInt(x.impact||2,10)));return `<div class="locked-item"><b>🚫 ${x.ativo||'ATIVO'}</b><br>${'🐂'.repeat(imp)} ${x.currency||''} — ${x.event||'Evento econômico'}<br><span style="color:#6f8095">Notícia: ${x.horario||'--:--'} | Liberação: ${x.liberacao||'--:--'}</span></div>`}).join(''):'<div class="empty">Nenhum ativo bloqueado por notícia no momento.</div>';
    const b2=document.getElementById('news-locked-list-2');if(b2)b2.innerHTML=itens.length?itens.map(x=>{const imp=Math.max(1,Math.min(3,parseInt(x.impact||2,10)));return `<div class="locked-item"><b>🚫 ${x.ativo||'ATIVO'}</b><br>${'🐂'.repeat(imp)} ${x.currency||''} — ${x.event||'Evento econômico'}<br><span style="color:#6f8095">Notícia: ${x.horario||'--:--'} | Liberação: ${x.liberacao||'--:--'}</span></div>`}).join(''):'<div class="empty">Nenhum ativo bloqueado por notícia no momento.</div>';
}
function setText(id,v){const e=document.getElementById(id);if(e)e.innerText=v}
function renderProbability(prob,id='prob-value',fill='prob-fill'){const p=Math.max(0,Math.min(100,Number(prob)||0));setText(id,p?p+'%':'--%');const e=document.getElementById(fill);if(e)e.style.width=p+'%'}
function renderSignal(d){
    const a=d.analise_atual||{};const alerta=d.alerta||{};const active=d.aguardando&&d.analise_atual?d.analise_atual:null;const dir=(active&&active.direcao)||a.direcao||null;const prob=(active&&active.probabilidade)||a.probabilidade||0;
    const ativo=(active&&active.ativo)||a.ativo||d.ativo_atual||'AGUARDANDO';
    setText('asset-tag',ativo);setText('analysis-asset',ativo);setText('signal-asset-2',ativo);setText('signal-tf', 'M'+(d.timeframe||5));setText('signal-tf-2','M'+(d.timeframe||5));
    const dirs=['signal-direction','signal-direction-2'];dirs.forEach(id=>{const e=document.getElementById(id);if(!e)return;e.className='signal-direction '+(dir==='CALL'?'signal-call':dir==='PUT'?'signal-put':'signal-wait');e.innerText=dir||'AGUARDANDO'});
    renderProbability(prob);renderProbability(prob,'prob-value-3','prob-fill-3');setText('prob-value-2',prob?prob+'%':'--%');setText('confluence-overall',a.confluencia!=null?'Confluência técnica: '+Number(a.confluencia).toFixed(0)+'/100':'Confluência técnica: --/100');setText('confluence-overall-2',a.confluencia!=null?'Confluência '+Number(a.confluencia).toFixed(0)+'/100':'Confluência --/100');
    const est=(active&&active.estrategia_fmt)||a.estrategia_fmt||'Motor aguardando análise';setText('signal-strategy',est);setText('signal-strategy-2',est);setText('analysis-direction',dir?dir:'Sem sinal');
    setText('signal-entry',d.entry_time||'--:--:--');setText('signal-entry-2',d.entry_time||'--:--:--');setText('signal-expiry',(active&&active.str_saida)||a.str_saida||'--:--');setText('signal-expiry-2',(active&&active.str_saida)||a.str_saida||'--:--');
    setText('signal-status',d.aguardando?(dir?('CONFIRMADO • '+ativo):'CONFIRMADO'):d.rodando?'ANALISANDO':'PARADO');setText('robot-status',d.rodando?'ONLINE':'PARADO');setText('top-status',d.rodando?'ANALISANDO':'ONLINE');
    renderConfluence(a,'confluence-list');renderReasons(a,'analysis-reasons');renderConfluence(a,'confluence-list-2');renderReasons(a,'analysis-reasons-2');drawChart(a.grafico||[],'market-chart');drawChart(a.grafico||[],'market-chart-2');
}
function renderConfluence(a,id){const box=document.getElementById(id);if(!box)return;const items=Array.isArray(a.confluencias)?a.confluencias:[];box.innerHTML=items.length?items.map(x=>`<div class="conf-row"><div class="conf-name">${x.nome||'Indicador'}</div><div class="conf-bar"><div class="conf-fill" style="width:${Math.max(0,Math.min(100,Number(x.pontos)||0))*5}%"></div></div><div class="conf-points">${x.pontos||0}/20</div></div>`).join(''):'<div class="empty">Aguardando dados do mercado...</div>'}
function renderReasons(a,id){const box=document.getElementById(id);if(!box)return;const items=Array.isArray(a.motivos)?a.motivos:[];box.innerHTML=items.length?items.map(x=>`<div class="reason"><b class="${x.status==='ok'?'ok':x.status==='warn'?'warn':'bad'}">${x.status==='ok'?'✓':x.status==='warn'?'•':'×'} ${x.nome||'Indicador'}</b><div>${x.detalhe||''}</div></div>`).join(''):'<div class="empty">Sem diagnóstico disponível.</div>'}
function drawChart(vals,id){const c=document.getElementById(id);if(!c)return;const ctx=c.getContext('2d');const rect=c.getBoundingClientRect();const w=Math.max(300,Math.floor(rect.width)),h=Math.max(120,Math.floor(rect.height));const dpr=window.devicePixelRatio||1;c.width=w*dpr;c.height=h*dpr;ctx.setTransform(dpr,0,0,dpr,0,0);ctx.clearRect(0,0,w,h);ctx.strokeStyle='#172333';ctx.lineWidth=1;for(let i=1;i<4;i++){const y=i*h/4;ctx.beginPath();ctx.moveTo(0,y);ctx.lineTo(w,y);ctx.stroke()}if(!Array.isArray(vals)||vals.length<2){ctx.fillStyle='#64748b';ctx.font='11px Inter';ctx.fillText('Aguardando candles válidos...',12,20);return}const min=Math.min(...vals),max=Math.max(...vals),range=max-min||1;const pts=vals.map((v,i)=>[i*(w-18)/(vals.length-1)+9,h-10-((v-min)/range)*(h-24)]);ctx.beginPath();pts.forEach((p,i)=>i?ctx.lineTo(p[0],p[1]):ctx.moveTo(p[0],p[1]));ctx.strokeStyle='#00d9ff';ctx.lineWidth=2.2;ctx.stroke();ctx.lineTo(pts[pts.length-1][0],h-10);ctx.lineTo(pts[0][0],h-10);ctx.closePath();ctx.fillStyle='rgba(0,217,255,.07)';ctx.fill();const last=pts[pts.length-1];ctx.beginPath();ctx.arc(last[0],last[1],3.5,0,Math.PI*2);ctx.fillStyle='#22c55e';ctx.fill()}
function renderHistory(hist){const body=document.getElementById('history-table-body');if(!body)return;if(!hist.length){body.innerHTML='<tr><td colspan="3" class="empty">Nenhum histórico.</td></tr>';return}body.innerHTML=hist.map(x=>`<tr><td>#${x.id||'--'}</td><td>${x.sinal||'--'}</td><td>${x.res||'--'}</td></tr>`).join('')}
function renderResumoHistorico(r){const make=(arr)=>arr&&arr.length?arr.map(x=>`<div class="history-item"><span>${x.nome}</span><b>${x.assertividade}% <span style="color:#66758a">(${x.wins}W/${x.reds}R)</span></b></div>`).join(''):'<div class="empty">Sem dados suficientes.</div>';const a=document.getElementById('strategy-summary'),b=document.getElementById('asset-summary');if(a)a.innerHTML=make((r||{}).estrategias||[]);if(b)b.innerHTML=make((r||{}).ativos||[])}
function atualizarSessao(d){setText('win-count',d.wins||0);setText('loss-count',d.reds||0);setText('wr-text',(d.winrate||0)+'%');setText('g1-count',d.g1_sessao||0);setText('session-count',(d.sinais_sessao_total||0)+' operações');const f=document.getElementById('wr-fill');if(f)f.style.width=Math.max(0,Math.min(100,Number(d.winrate)||0))+'%'}
async function atualizarPainel(){try{const r=await fetch('/status',{cache:'no-store'});const d=await r.json();if(d.redirect){location.href=d.redirect;return}latestData=d;renderSignal(d);atualizarSessao(d);atualizarAtivosBloqueados(d.news_blocked_assets||[]);const ng=d.news_guard_status||'AGUARDANDO CALENDÁRIO';setText('news-guard-status',ng);setText('guard-detail-status',ng);const blocked=(d.news_blocked_assets||[]).length;const color=blocked?'#fb7185':ng.includes('INDISPONÍVEL')?'#fbbf24':'#86efac';['news-guard-status','guard-detail-status'].forEach(id=>{const e=document.getElementById(id);if(e)e.style.color=color});const b=document.getElementById('guard-badge');if(b)b.innerText=blocked?'● PROTEGENDO':'● ATIVO';const b2=document.getElementById('guard-badge-2');if(b2)b2.innerText=blocked?'● PROTEGENDO':'● ATIVO';const result=document.getElementById('result-area');if(result)result.style.display=d.aguardando?'grid':'none';renderHistory(d.historico||[]);renderResumoHistorico(d.historico_resumo||{});if(d.notificacao&&d.notificacao.id!==lastNotifId){lastNotifId=d.notificacao.id;dispararNotificacaoNativa(d.notificacao.titulo,d.notificacao.corpo,d.notificacao.id)}}catch(e){setText('top-status','REDE');}finally{setTimeout(atualizarPainel,1000)}}
window.addEventListener('resize',()=>{if(latestData&&latestData.analise_atual){drawChart(latestData.analise_atual.grafico||[],'market-chart');drawChart(latestData.analise_atual.grafico||[],'market-chart-2')}});
atualizarPainel();
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

def resumir_historico(historico):
    """Resume os últimos registros por estratégia e ativo sem alterar o banco legado."""
    por_estrategia = {}
    por_ativo = {}
    for item in historico or []:
        sinal = str(item.get("sinal", ""))
        resultado = str(item.get("res", "")).lower()
        partes = [p.strip() for p in sinal.split("|")]
        ativo = partes[0] if partes else "OUTRO"
        estrategia = partes[2] if len(partes) >= 3 else "Não informado"
        for mapa, chave in ((por_estrategia, estrategia), (por_ativo, ativo)):
            reg = mapa.setdefault(chave, {"total": 0, "wins": 0, "reds": 0})
            reg["total"] += 1
            if "win" in resultado:
                reg["wins"] += 1
            elif "red" in resultado:
                reg["reds"] += 1
    def finalizar(mapa):
        out=[]
        for nome, reg in mapa.items():
            concl=reg["wins"]+reg["reds"]
            out.append({"nome":nome,"total":reg["total"],"wins":reg["wins"],"reds":reg["reds"],"assertividade":round(reg["wins"]/concl*100,1) if concl else 0})
        return sorted(out,key=lambda x:(-x["total"],-x["assertividade"],x["nome"]))[:8]
    return {"estrategias":finalizar(por_estrategia),"ativos":finalizar(por_ativo)}

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

# ================= TRAVA DE NOTÍCIAS / CALENDÁRIO INVESTING.COM =================
# O Investing.com classifica o impacto dos eventos com 1, 2 ou 3 estrelas/touros.
# 1 = baixo, 2 = moderado e 3 = alto. A proteção do Vision Pro usa somente 2 e 3.
NEWS_MIN_IMPACT = 2
NEWS_LOCK_BEFORE_MIN = 30
NEWS_LOCK_AFTER_MIN = 30
NEWS_CACHE_TTL = 60

# IMPORTANTE: se o Investing.com estiver temporariamente indisponível, o bot NÃO
# bloqueia todos os ativos. Ele continua a análise normal e tenta consultar a fonte
# novamente no próximo ciclo. Assim, somente uma notícia realmente identificada
# pelo calendário pode bloquear um ativo.
NEWS_FAIL_OPEN = True

INVESTING_CALENDAR_CACHE = {
    "updated": 0.0,
    "events": [],
    "ok": False,
    "error": "",
}
INVESTING_CALENDAR_LOCK = threading.Lock()

# Códigos de países usados pelo calendário do Investing.com para as moedas dos ativos.
# O código 12 é GMT -3:00 (horário de Brasília) no calendário do Investing.com.
INVESTING_COUNTRIES = "5,4,72,35,25,6,12,43"
INVESTING_TIMEZONE = "12"


def _limpar_html_investing(valor):
    if not valor:
        return ""
    valor = re.sub(r"<script[^>]*>.*?</script>", " ", str(valor), flags=re.I | re.S)
    valor = re.sub(r"<style[^>]*>.*?</style>", " ", valor, flags=re.I | re.S)
    valor = re.sub(r"<[^>]+>", " ", valor)
    return re.sub(r"\s+", " ", html_lib.unescape(valor)).strip()


def _parsear_datetime_investing(valor, origem="local"):
    """Converte data/hora do Investing.com sem deslocar 3 horas por engano."""
    if valor is None or valor == "":
        return None

    bruto = str(valor).strip()

    # event_timestamp pode aparecer como timestamp Unix em algumas respostas.
    if re.fullmatch(r"\d{10}(?:\.\d+)?", bruto):
        try:
            return datetime.fromtimestamp(float(bruto), tz=pytz.utc).astimezone(FUSO_SP)
        except Exception:
            return None

    # Algumas versões usam ISO com timezone.
    try:
        iso = bruto.replace("Z", "+00:00")
        dt_iso = datetime.fromisoformat(iso)
        if dt_iso.tzinfo is not None:
            return dt_iso.astimezone(FUSO_SP)
    except Exception:
        pass

    for fmt in (
        "%Y/%m/%d %H:%M:%S",
        "%Y-%m-%d %H:%M:%S",
        "%Y/%m/%d %H:%M",
        "%Y-%m-%d %H:%M",
    ):
        try:
            dt = datetime.strptime(bruto, fmt)
            if origem == "utc":
                return pytz.utc.localize(dt).astimezone(FUSO_SP)
            # Com timeZone=12, o Investing devolve o horário do calendário em GMT-3.
            return FUSO_SP.localize(dt)
        except ValueError:
            continue
    return None


def _extrair_eventos_investing(html_resposta):
    """Extrai somente eventos de 2 ou 3 estrelas/touros do calendário."""
    if not html_resposta:
        return []

    texto = html_resposta.decode("utf-8", errors="ignore") if isinstance(html_resposta, bytes) else str(html_resposta)

    # O endpoint Service retorna JSON com o HTML dentro de data.
    try:
        obj = json.loads(texto)
        if isinstance(obj, dict) and obj.get("data"):
            texto = obj["data"]
    except Exception:
        pass

    # Aceita tanto js-event-item quanto eventRowId_ (variações do Investing).
    rows = re.findall(
        r"<tr\b[^>]*(?:class=[\"'][^\"']*js-event-item[^\"']*|id=[\"']eventRowId_[^\"']+)[^>]*>.*?</tr>",
        texto,
        flags=re.I | re.S,
    )

    # Fallback mais amplo para respostas do widget/espelho.
    if not rows:
        rows = re.findall(r"<tr\b[^>]*id=[\"'][^\"']*eventRowId[^\"']*[\"'][^>]*>.*?</tr>", texto, flags=re.I | re.S)

    eventos = []
    vistos = set()

    for row in rows:
        # data-event-datetime normalmente representa o horário exibido pelo calendário.
        m_dt = re.search(r'data-event-datetime=[\"\']([^\"\']+)', row, flags=re.I)
        if m_dt:
            dt_evento = _parsear_datetime_investing(m_dt.group(1), origem="local")
        else:
            m_ts = re.search(r'event_timestamp=[\"\']([^\"\']+)', row, flags=re.I)
            dt_evento = _parsear_datetime_investing(m_ts.group(1) if m_ts else "", origem="utc")
        if not dt_evento:
            continue

        # Moeda: o Investing usa td.flagCur e, em algumas versões, title/data-attr.
        m_cur = re.search(
            r'<td[^>]*class=["\'][^"\']*flagCur[^"\']*["\'][^>]*>(.*?)</td>',
            row,
            flags=re.I | re.S,
        )
        currency_text = _limpar_html_investing(m_cur.group(1) if m_cur else "")
        currencies = re.findall(r"\b[A-Z]{3}\b", currency_text.upper())
        currency = currencies[0] if currencies else ""
        if not currency and m_cur:
            m_title = re.search(r'(?:title|data-currency)=["\']([A-Za-z]{3})["\']', m_cur.group(1), flags=re.I)
            currency = m_title.group(1).upper() if m_title else ""

        # Impacto: 2/3 grayFullBullishIcon = 2/3 touros/estrelas.
        m_sent = re.search(
            r'<td[^>]*class=["\'][^"\']*sentiment[^"\']*["\'][^>]*>(.*?)</td>',
            row,
            flags=re.I | re.S,
        )
        sentiment_html = m_sent.group(1) if m_sent else ""
        impacto = len(re.findall(r"grayFullBullishIcon", sentiment_html, flags=re.I))

        # Fallbacks para versões que expõem bull1/bull2/bull3 ou quantidade de ícones.
        if impacto <= 0:
            m_bull = re.search(r'data-img_key=["\']bull([1-3])["\']', sentiment_html, flags=re.I)
            impacto = int(m_bull.group(1)) if m_bull else 0
        if impacto <= 0:
            bull_classes = re.findall(r"bull(?:ish)?(?:Icon)?(?:[ _-]?(?:1|2|3))?", sentiment_html, flags=re.I)
            if bull_classes:
                impacto = min(3, len(bull_classes))

        if not currency or impacto < NEWS_MIN_IMPACT:
            continue

        m_event = re.search(
            r'<td[^>]*class=["\'][^"\']*event[^"\']*["\'][^>]*>(.*?)</td>',
            row,
            flags=re.I | re.S,
        )
        nome_evento = _limpar_html_investing(m_event.group(1) if m_event else "") or "Evento econômico"

        chave = (dt_evento.isoformat(), currency, impacto, nome_evento)
        if chave in vistos:
            continue
        vistos.add(chave)
        eventos.append({
            "datetime": dt_evento,
            "currency": currency,
            "impact": impacto,
            "event": nome_evento,
        })

    eventos.sort(key=lambda x: x["datetime"])
    return eventos


def atualizar_calendario_investing(force=False):
    """Consulta o Investing.com e mantém apenas eventos reais de 2/3 touros."""
    agora_ts = time.time()
    with INVESTING_CALENDAR_LOCK:
        if not force and (agora_ts - INVESTING_CALENDAR_CACHE.get("updated", 0)) < NEWS_CACHE_TTL:
            return INVESTING_CALENDAR_CACHE.get("ok", False)

        hoje = agora_brasilia().date()
        amanha = hoje + timedelta(days=1)
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/140.0.0.0 Safari/537.36"
            ),
            "Accept": "text/html,application/json;q=0.9,*/*;q=0.8",
            "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
            "X-Requested-With": "XMLHttpRequest",
            "Origin": "https://www.investing.com",
            "Referer": "https://www.investing.com/economic-calendar/",
        }

        eventos = []
        erro = ""
        sucesso_fonte = False

        # 1) Endpoint oficial usado pelo calendário.
        urls_api = [
            "https://br.investing.com/economic-calendar/Service/getCalendarFilteredData",
            "https://www.investing.com/economic-calendar/Service/getCalendarFilteredData",
        ]

        for url in urls_api:
            try:
                with requests.Session() as sess:
                    base = url.split("/Service/")[0] + "/"
                    sess.headers.update({"User-Agent": headers["User-Agent"], "Accept-Language": headers["Accept-Language"]})
                    try:
                        sess.get(base, headers={"User-Agent": headers["User-Agent"], "Accept-Language": headers["Accept-Language"]}, timeout=8)
                    except Exception:
                        pass

                    payload = {
                        "country[]": INVESTING_COUNTRIES.split(","),
                        "dateFrom": hoje.strftime("%Y-%m-%d"),
                        "dateTo": amanha.strftime("%Y-%m-%d"),
                        "timeZone": INVESTING_TIMEZONE,
                        "timeFilter": "timeRemain",
                        "currentTab": "custom",
                        "submitFilters": "1",
                        "limit_from": "0",
                    }
                    resp = sess.post(url, data=payload, headers=headers, timeout=12)
                    if resp.status_code == 200 and resp.text:
                        sucesso_fonte = True
                        eventos = _extrair_eventos_investing(resp.text)
                        # Resposta válida sem eventos de 2/3 touros é normal.
                        break
                    erro = f"HTTP {resp.status_code} em {url}"
            except Exception as exc:
                erro = str(exc)

        # 2) Página oficial do calendário como fallback.
        if not sucesso_fonte:
            for url in ("https://br.investing.com/economic-calendar/", "https://www.investing.com/economic-calendar/"):
                try:
                    resp = requests.get(url, headers={**headers, "X-Requested-With": ""}, timeout=12)
                    if resp.status_code == 200 and resp.text:
                        sucesso_fonte = True
                        eventos = _extrair_eventos_investing(resp.text)
                        break
                    erro = f"HTTP {resp.status_code} em {url}"
                except Exception as exc:
                    erro = str(exc)

        # 3) Widget oficial do Investing.com: alternativa quando o endpoint principal
        # estiver protegido/indisponível no servidor do Render.
        if not sucesso_fonte:
            widget_url = (
                "https://sslecal2.investing.com/?"
                "columns=exc_flags,exc_currency,exc_importance,exc_actual,exc_forecast,exc_previous&"
                "features=datepicker,timezone&"
                f"countries={INVESTING_COUNTRIES}&"
                "calType=week&"
                f"timeZone={INVESTING_TIMEZONE}&"
                "lang=12"
            )
            try:
                resp = requests.get(widget_url, headers={"User-Agent": headers["User-Agent"], "Accept-Language": headers["Accept-Language"]}, timeout=12)
                if resp.status_code == 200 and resp.text:
                    sucesso_fonte = True
                    eventos = _extrair_eventos_investing(resp.text)
            except Exception as exc:
                erro = str(exc)

        INVESTING_CALENDAR_CACHE["updated"] = agora_ts

        if sucesso_fonte:
            INVESTING_CALENDAR_CACHE["events"] = eventos
            INVESTING_CALENDAR_CACHE["ok"] = True
            INVESTING_CALENDAR_CACHE["error"] = ""
        else:
            # Não limpa eventos antigos aqui para facilitar diagnóstico, mas o motor
            # não usa cache antigo quando a fonte não confirmou uma atualização.
            INVESTING_CALENDAR_CACHE["ok"] = False
            INVESTING_CALENDAR_CACHE["error"] = erro or "Fonte indisponível"

        return sucesso_fonte


def moedas_do_ativo(ativo):
    base = str(ativo or "").upper().replace("-OTC", "")
    cripto = {x.replace("-OTC", "") for x in (ATIVOS_BASE["CRIPTO_ABERTO"] + ATIVOS_BASE["CRIPTO_OTC"])}
    if base in cripto:
        # Notícias macro de USD são aplicadas aos ativos de cripto cotados em USD.
        return ["USD"]
    if len(base) >= 6:
        return [base[:3], base[3:6]]
    return []


def evento_bloqueia_ativo(ativo, agora, eventos=None):
    """Retorna o evento de 2/3 touros que está dentro da janela do ativo."""
    moedas = set(moedas_do_ativo(ativo))
    if not moedas:
        return None

    eventos = eventos if eventos is not None else INVESTING_CALENDAR_CACHE.get("events", [])
    inicio_janela = timedelta(minutes=NEWS_LOCK_BEFORE_MIN)
    fim_janela = timedelta(minutes=NEWS_LOCK_AFTER_MIN)
    melhor = None
    distancia_melhor = None

    for evento in eventos:
        if evento.get("currency") not in moedas or int(evento.get("impact", 0)) < NEWS_MIN_IMPACT:
            continue
        dt_evento = evento.get("datetime")
        if not dt_evento:
            continue
        if not (dt_evento - inicio_janela <= agora <= dt_evento + fim_janela):
            continue
        distancia = abs((agora - dt_evento).total_seconds())
        if distancia_melhor is None or distancia < distancia_melhor:
            distancia_melhor = distancia
            melhor = evento

    return melhor


def ativo_bloqueado_por_noticia(ativo, agora=None):
    """Compatibilidade: verifica um ativo sem bloquear por falha da fonte."""
    agora = agora or agora_brasilia()
    calendario_ok = atualizar_calendario_investing()
    if not calendario_ok and NEWS_FAIL_OPEN:
        return False, None
    evento = evento_bloqueia_ativo(ativo, agora)
    return evento is not None, evento


def resumo_trava_noticias(evento):
    if not evento:
        return ""
    dt = evento.get("datetime")
    horario = dt.strftime("%H:%M") if hasattr(dt, "strftime") else "--:--"
    impacto = int(evento.get("impact", NEWS_MIN_IMPACT))
    touros = "🐂" * max(1, min(3, impacto))
    return f"🔒 {touros} {evento.get('currency', '')} — {evento.get('event', 'Evento')} às {horario} | trava ±30 min"

# ================= MOTOR DE ANÁLISE REAL DE 30 VELAS =================
def _normalizar_candles_fechados(ohlc, tf):
    """Remove candles incompletos e valores inválidos antes da análise."""
    try:
        n = len(ohlc.get("close", []))
        if n < 2:
            return None
        mask = np.isfinite(ohlc["open"]) & np.isfinite(ohlc["high"]) & np.isfinite(ohlc["low"]) & np.isfinite(ohlc["close"])
        for k in ohlc:
            ohlc[k] = np.asarray(ohlc[k])[mask]
        if len(ohlc["close"]) < 2:
            return None
        # A análise usa apenas candles fechados. O último candle é descartado
        # quando ainda estiver dentro da janela corrente do timeframe.
        ultimo_ts = float(ohlc["time"][-1])
        agora_ts = time.time()
        if ultimo_ts + (int(tf) * 60) > agora_ts:
            for k in ohlc:
                ohlc[k] = ohlc[k][:-1]
        return ohlc if len(ohlc["close"]) >= 30 else None
    except Exception:
        return None

def get_data_v2(ticker, tf, velas_minimas=30):
    """Obtém candles reais. Nunca cria candles aleatórios quando uma fonte falha."""
    try:
        base_ticker = ticker
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/122 Safari/537.36',
            'Accept': 'application/json, text/plain, */*'
        }
        url = f"https://query2.finance.yahoo.com/v8/finance/chart/{base_ticker}?interval={tf}m&range=5d"
        res = requests.get(url, headers=headers, timeout=5.0)
        if res.status_code == 200:
            payload = res.json()
            result = (payload.get('chart') or {}).get('result')
            if result:
                result = result[0]
                timestamps = result.get('timestamp') or []
                quote = (result.get('indicators') or {}).get('quote', [{}])[0]
                ohlc = {
                    "time": np.array(timestamps),
                    "open": np.array(quote.get('open', []), dtype=float),
                    "high": np.array(quote.get('high', []), dtype=float),
                    "low": np.array(quote.get('low', []), dtype=float),
                    "close": np.array(quote.get('close', []), dtype=float)
                }
                fechado = _normalizar_candles_fechados(ohlc, tf)
                if fechado is not None and len(fechado["close"]) >= velas_minimas:
                    return fechado

        if "-USD" in base_ticker or "USD" in ticker:
            crypto_symbol = ticker.replace("USD", "").replace("-OTC", "").replace("-", "")
            url_alt = f"https://min-api.cryptocompare.com/data/v2/histominute?fsym={crypto_symbol}&tsym=USD&limit=300&aggregate={tf}"
            r_alt = requests.get(url_alt, timeout=5.0)
            if r_alt.status_code == 200:
                payload = r_alt.json()
                data_list = (payload.get('Data') or {}).get('Data') or []
                if data_list:
                    ohlc = {
                        "time": np.array([x.get('time', 0) for x in data_list]),
                        "open": np.array([x.get('open', np.nan) for x in data_list], dtype=float),
                        "high": np.array([x.get('high', np.nan) for x in data_list], dtype=float),
                        "low": np.array([x.get('low', np.nan) for x in data_list], dtype=float),
                        "close": np.array([x.get('close', np.nan) for x in data_list], dtype=float)
                    }
                    fechado = _normalizar_candles_fechados(ohlc, tf)
                    if fechado is not None and len(fechado["close"]) >= velas_minimas:
                        return fechado
        return None
    except Exception as e:
        print(f"⚠️ Falha ao obter dados reais de {ticker}: {e}")
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
def _rsi_atual(c, periodo=14):
    if len(c) < periodo + 1:
        return 50.0
    delta = np.diff(c)
    ganhos = np.where(delta > 0, delta, 0.0)
    perdas = np.where(delta < 0, -delta, 0.0)
    ganho = np.mean(ganhos[-periodo:])
    perda = np.mean(perdas[-periodo:])
    if perda <= 1e-12:
        return 100.0 if ganho > 0 else 50.0
    rs = ganho / perda
    return float(100 - (100 / (1 + rs)))

def _macd_atual(c):
    ema12 = calcular_ema(c, 12)
    ema26 = calcular_ema(c, 26)
    linha = ema12 - ema26
    sinal = calcular_ema(linha, 9)
    return float(linha[-1]), float(sinal[-1]), float(linha[-1] - sinal[-1])

def _indicadores_confluencia(data, direcao=None):
    c, o, h, l = data["close"], data["open"], data["high"], data["low"]
    ema9 = calcular_ema(c, 9)
    ema21 = calcular_ema(c, 21)
    rsi = _rsi_atual(c, 14)
    macd, macd_signal, macd_hist = _macd_atual(c)
    ma20 = np.mean(c[-20:])
    std20 = np.std(c[-20:])
    bb_sup, bb_inf = ma20 + 2 * std20, ma20 - 2 * std20
    tr = np.maximum(h[-20:] - l[-20:], np.maximum(np.abs(h[-20:] - c[-21:-1]), np.abs(l[-20:] - c[-21:-1]))) if len(c) >= 21 else h[-20:] - l[-20:]
    atr = float(np.mean(tr)) if len(tr) else 0.0
    preco = float(c[-1])
    atr_pct = (atr / preco * 100) if preco else 0.0
    corpo = abs(c[-1] - o[-1])
    amplitude = max(h[-1] - l[-1], 1e-12)
    pavio_sup = h[-1] - max(o[-1], c[-1])
    pavio_inf = min(o[-1], c[-1]) - l[-1]
    suporte = float(np.min(l[-20:-1]))
    resistencia = float(np.max(h[-20:-1]))
    tendencia = 'ALTA' if ema9[-1] > ema21[-1] and ema21[-1] >= ema21[-4] else ('BAIXA' if ema9[-1] < ema21[-1] and ema21[-1] <= ema21[-4] else 'LATERAL')

    itens=[]
    def add(nome,pontos,detalhe,status): itens.append({"nome":nome,"pontos":int(max(0,min(20,pontos))),"detalhe":detalhe,"status":status})

    # Tendência
    if direcao == 'CALL':
        ok=tendencia=='ALTA'; pts=20 if ok else (10 if tendencia=='LATERAL' else 3)
        add('Tendência',pts,f"EMA9 {('acima' if ema9[-1]>ema21[-1] else 'abaixo')} da EMA21 • {tendencia}",'ok' if ok else 'warn' if tendencia=='LATERAL' else 'bad')
    elif direcao == 'PUT':
        ok=tendencia=='BAIXA'; pts=20 if ok else (10 if tendencia=='LATERAL' else 3)
        add('Tendência',pts,f"EMA9 {('abaixo' if ema9[-1]<ema21[-1] else 'acima')} da EMA21 • {tendencia}",'ok' if ok else 'warn' if tendencia=='LATERAL' else 'bad')
    else:
        add('Tendência',20 if tendencia!='LATERAL' else 10,f"Mercado em {tendencia}",'ok' if tendencia!='LATERAL' else 'warn')

    # RSI
    if direcao=='CALL':
        ok=45 <= rsi <= 68; pts=18 if ok else (11 if 35<=rsi<45 or 68<rsi<=75 else 5)
    elif direcao=='PUT':
        ok=32 <= rsi <= 55; pts=18 if ok else (11 if 25<=rsi<32 or 55<rsi<=65 else 5)
    else: ok=False; pts=10
    add('RSI',pts,f"RSI {rsi:.1f}",'ok' if ok else 'warn')

    # MACD
    macd_ok=(macd_hist>0) if direcao=='CALL' else ((macd_hist<0) if direcao=='PUT' else False)
    add('MACD',18 if macd_ok else 7,f"Histograma {'positivo' if macd_hist>0 else 'negativo'}",'ok' if macd_ok else 'warn')

    # Price action
    bullish = c[-1] > o[-1]
    bearish = c[-1] < o[-1]
    rejection = (pavio_inf/amplitude >= .35) if direcao=='CALL' else ((pavio_sup/amplitude >= .35) if direcao=='PUT' else False)
    pa_ok = (bullish if direcao=='CALL' else bearish if direcao=='PUT' else False) or rejection
    pa_pts=18 if pa_ok else 7
    add('Price Action',pa_pts,f"Corpo {corpo/amplitude*100:.0f}% • {'rejeição detectada' if rejection else 'candle direcional'}",'ok' if pa_ok else 'warn')

    # Suporte / resistência
    dist_sup=abs(preco-suporte)/(preco or 1)*100
    dist_res=abs(resistencia-preco)/(preco or 1)*100
    sr_ok=(dist_sup <= max(0.15, atr_pct*1.4)) if direcao=='CALL' else ((dist_res <= max(0.15, atr_pct*1.4)) if direcao=='PUT' else False)
    add('Suporte/Resist.',15 if sr_ok else 7,f"Sup {dist_sup:.2f}% • Res {dist_res:.2f}%",'ok' if sr_ok else 'warn')

    # Volatilidade
    vol_ok = 0.02 <= atr_pct <= 1.8
    add('Volatilidade',11 if vol_ok else 5,f"ATR {atr_pct:.3f}% do preço",'ok' if vol_ok else 'warn')

    # Banda de Bollinger
    bb_ok=(preco<=bb_inf*1.003) if direcao=='CALL' else ((preco>=bb_sup*.997) if direcao=='PUT' else False)
    add('Bollinger',12 if bb_ok else 7,f"Preço {'próximo da banda inferior' if preco<=bb_inf else 'próximo da banda superior' if preco>=bb_sup else 'dentro das bandas'}",'ok' if bb_ok else 'warn')

    # Probabilidade heuristicamente calibrada sobre a estratégia existente.
    soma=sum(x['pontos'] for x in itens); maximo=len(itens)*20
    confluencia=round((soma/maximo)*100,1) if maximo else 0.0
    return {
        'rsi':rsi,'ema9':float(ema9[-1]),'ema21':float(ema21[-1]),'macd':macd,'macd_signal':macd_signal,'macd_hist':macd_hist,
        'atr_pct':atr_pct,'tendencia':tendencia,'suporte':suporte,'resistencia':resistencia,
        'confluencia':confluencia,'confluencias':itens
    }

def analisar_estrategia(data, estrategia, i=-1):
    """Motor legado preservado para compatibilidade; retorna sinal e probabilidade em %."""
    c, o, h, l = data["close"], data["open"], data["high"], data["low"]
    if len(c) < 30:
        return None, 0
    sinal=None; probabilidade=0
    if estrategia == "LOGICA_DO_PRECO":
        tamanho=abs(c[i]-o[i]); amplitude=h[i]-l[i]
        if amplitude>0 and tamanho>0:
            cor='G' if c[i]>o[i] else 'R'; p_sup=h[i]-max(o[i],c[i]); p_inf=min(o[i],c[i])-l[i]
            if cor=='G' and p_inf>=amplitude*.45 and p_sup<=amplitude*.20: sinal='CALL'; probabilidade=int(82+(p_inf/amplitude)*15)
            elif cor=='R' and p_sup>=amplitude*.45 and p_inf<=amplitude*.20: sinal='PUT'; probabilidade=int(82+(p_sup/amplitude)*15)
            elif cor=='G' and p_sup>=amplitude*.50 and tamanho<=amplitude*.35: sinal='PUT'; probabilidade=int(80+(p_sup/amplitude)*15)
            elif cor=='R' and p_inf>=amplitude*.50 and tamanho<=amplitude*.35: sinal='CALL'; probabilidade=int(80+(p_inf/amplitude)*15)
    elif estrategia == "RSI_MACD_MA":
        rsi=_rsi_atual(c,14); macd_line,signal_line,_=_macd_atual(c)
        if rsi<=35 and macd_line>signal_line: sinal='CALL'; probabilidade=int(83+(35-rsi)*.5)
        elif rsi>=65 and macd_line<signal_line: sinal='PUT'; probabilidade=int(83+(rsi-65)*.5)
    elif estrategia == "MHI1":
        cores=[]
        for j in range(i-2,i+1): cores.append('G' if c[j]>o[j] else 'R' if c[j]<o[j] else 'D')
        if 'D' not in cores:
            qtd_g=cores.count('G');qtd_r=cores.count('R');ema20=np.mean(c[-20:])
            if qtd_g==2 and qtd_r==1 and c[i]<=ema20: sinal='PUT';probabilidade=84
            elif qtd_r==2 and qtd_g==1 and c[i]>=ema20: sinal='CALL';probabilidade=84
            elif qtd_g==3: sinal='PUT';probabilidade=88
            elif qtd_r==3: sinal='CALL';probabilidade=88
    elif estrategia in ['REVERSAO','RETRACAO']:
        std=np.std(c[-20:]);ma=np.mean(c[-20:]);bs=ma+2*std;bi=ma-2*std
        if c[i]<=bi and c[i]<o[i]: sinal='CALL';dist=(bi-c[i])/(std if std>0 else 1);probabilidade=int(81+min(15,dist*10))
        elif c[i]>=bs and c[i]>o[i]: sinal='PUT';dist=(c[i]-bs)/(std if std>0 else 1);probabilidade=int(81+min(15,dist*10))
    probabilidade=min(98,max(75,probabilidade)) if sinal else 0
    return sinal,probabilidade

def analisar_estrategia_detalhada(data, estrategia):
    sinal, base_prob = analisar_estrategia(data, estrategia)
    indicadores = _indicadores_confluencia(data, sinal)
    if not sinal:
        return None, 0, indicadores
    # Ajuste moderado baseado nas confluências, mantendo a faixa histórica do Vision Pro.
    ajuste = round((indicadores['confluencia'] - 65) * 0.12)
    prob = int(max(75, min(98, base_prob + ajuste)))
    return sinal, prob, indicadores

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
        "historico_resumo": resumir_historico(historico),
        "ativo_atual": st["ativo_atual"],
        "news_guard_status": st.get("news_guard_status", "AGUARDANDO CALENDÁRIO"),
        "news_guard_event": st.get("news_guard_event"),
        "news_blocked_assets": st.get("news_blocked_assets", []),
        "analise_atual": st.get("analise_atual"),
        "alerta": st.get("alerta_ativo"),
        "sinais_sessao_total": st.get("sinais_sessao_total", 0),
        "g1_sessao": sum(1 for r in st.get("sessao_resultados", []) if r == "g1"),
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
            f"🧪 <b>TESTE DE COMUNICAÇÃO - VISION PRO V4</b>\n\n"
            f"✅ Conexão estabelecida com sucesso com o Telegram!\n"
            f"👤 Usuário: Vision Pro\n"
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
        st["telegram_alert_status"] = {}
        st["ultima_confirmacao_msg_id"] = None
        st["ultima_confirmacao_alert_id"] = None
        st["sessao_resultados"] = []
        st["news_guard_status"] = "CONSULTANDO INVESTING.COM"
        st["news_guard_event"] = None
        st["news_blocked_assets"] = []
        st["news_guard_updated"] = 0.0
        st["analise_atual"] = None
        st["sinais_sessao_total"] = 0
        st["inicio_varredura"] = time.time() + 2 
        st["sinais_enviados"].clear() 
        
        st["ativo_atual"] = "INICIANDO VARREDURA..."
        st["ultimo_sinal"] = f"<div class='system-console'>⚡ <b>INICIANDO MOTOR DE ANÁLISE DINÂMICA</b><br><span style='color:#00f2fe;'>[VARRENDO TODOS OS ATIVOS...]</span></div><div class='tech-scanner'></div>"
        
        msg_inicio_telegram = (
            f"🚀 <b>SISTEMA VISION PRO V4 INICIADO</b>\n\n"
            f"🟢 <b>Status:</b> Análise de 30 velas ativada\n"
            f"👤 <b>Usuário:</b> Vision Pro\n"
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
        cancelar_alerta_telegram(st, st.get("alerta_ativo"))
        st["alerta_ativo"] = None
        # Envia o fechamento ANTES de limpar os resultados da sessão.
        enviar_telegram(mensagem_encerramento_sessao(st), user_solicitante=user)

        st["ativo_atual"] = "DESCONECTADO"
        st["news_guard_status"] = "DESATIVADA"
        st["news_guard_event"] = None
        st["news_blocked_assets"] = []
        st["ultimo_sinal"] = "Aguardando Comando..."
        
        # Mantém o comportamento anterior de zerar o placar geral no encerramento.
        zerar_estatisticas_usuario(user)
        st["sessao_resultados"] = []
        st["analise_atual"] = None
        st["sinais_sessao_total"] = 0
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
            "📊 Resultado registrado no Vision Pro.\n"
            "⚠️ O resultado de uma operação não garante resultados futuros.\n\n"
            f"📊 Placar Geral: {placar}"
        )
    if resultado == "g1":
        return (
            "🔄 <b>VITÓRIA CONFIRMADA NO GALE 1!</b> 🔄\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "📊 Resultado registrado como G1.\n"
            "⚠️ Gerencie o risco e não trate o resultado como garantia.\n\n"
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
        # Capture references before the panel clears the active signal.
        alerta_atual = st.get("alerta_ativo")
        confirmacao_msg_id = st.get("ultima_confirmacao_msg_id")

        if res in ("win", "g1", "red", "pular"):
            # Qualquer resultado encerra o ciclo do alerta atual.
            cancelar_alerta_telegram(st, alerta_atual)

        if res == 'win':
            atualizar_estatisticas_usuario(user, True)
            atualizar_ultimo_sinal_bd(user, "Win")
            registrar_resultado_sessao(st, "win")
            st["sinais_sessao_total"] = st.get("sinais_sessao_total", 0) + 1
            enviar_telegram(mensagem_resultado_telegram(st, "win"), user_solicitante=user)
        elif res == 'g1':
            atualizar_estatisticas_usuario(user, True)
            atualizar_ultimo_sinal_bd(user, "WinG1")
            registrar_resultado_sessao(st, "g1")
            st["sinais_sessao_total"] = st.get("sinais_sessao_total", 0) + 1
            enviar_telegram(mensagem_resultado_telegram(st, "g1"), user_solicitante=user)
        elif res == 'red':
            atualizar_estatisticas_usuario(user, False)
            atualizar_ultimo_sinal_bd(user, "Red")
            registrar_resultado_sessao(st, "red")
            st["sinais_sessao_total"] = st.get("sinais_sessao_total", 0) + 1
            enviar_telegram(mensagem_resultado_telegram(st, "red"), user_solicitante=user)
        elif res == 'pular':
            # Se o sinal já foi confirmado, apagar a confirmação anterior.
            if confirmacao_msg_id:
                deletar_mensagem_telegram(confirmacao_msg_id)
                st["ultima_confirmacao_msg_id"] = None
                st["ultima_confirmacao_alert_id"] = None

            # O aviso de PULADO permanece somente por 5 segundos.
            enviar_telegram(
                "⚠️ <b>SINAL IGNORADO / PULADO</b>",
                auto_delete=5,
                user_solicitante=user
            )

        st["aguardando_confirmacao"] = False
        st["sinal_permanente"] = None
        if st.get("timer_confirmacao"):
            try:
                st["timer_confirmacao"].cancel()
            except Exception:
                pass
        st["timer_confirmacao"] = None
        st["alerta_ativo"] = None
        st["analise_atual"] = None
        
        st["ultimo_sinal"] = f"<div class='system-console'>🔍 ANALISANDO VELAS: <b>{st['ativo_atual']}</b> (M{st['timeframe']})<br><span style='color:#00f2fe;'>[RETOMANDO VARREDURA COMPLETA]</span></div><div class='tech-scanner'></div>"
    
    return redirect('/')


# ================= ENVIO TELEGRAM ASSÍNCRONO =================
def enviar_telegram_em_background(mensagem, user_email, alert_id=None, deletar_msg_id=None, st=None):
    """Envia alerta em background e evita que um alerta cancelado reapareça.
    Se o alerta for cancelado durante o envio, a mensagem recém-enviada é
    apagada imediatamente para manter o Telegram sincronizado com o painel.
    """
    def worker():
        try:
            if deletar_msg_id:
                try:
                    deletar_mensagem_telegram(deletar_msg_id)
                except Exception as e:
                    print(f"⚠️ Falha ao deletar alerta antigo no Telegram: {e}")

            if st is not None and alert_id is not None:
                st.setdefault("telegram_alert_status", {})[alert_id] = "active"

            novo_id = enviar_telegram(mensagem, auto_delete=None, user_solicitante=user_email)
            if not novo_id:
                return

            if st is not None and alert_id is not None:
                status = st.setdefault("telegram_alert_status", {}).get(alert_id, "cancelled")
                atual = st.get("alerta_ativo")
                if status != "active" or (atual and atual.get("alert_id") != alert_id):
                    # O alerta foi confirmado, pulado, substituído ou cancelado
                    # enquanto a requisição ao Telegram estava em andamento.
                    deletar_mensagem_telegram(novo_id)
                    return
                atual["msg_id"] = novo_id
        except Exception as e:
            print(f"⚠️ Erro no envio Telegram em background: {e}")
    threading.Thread(target=worker, daemon=True).start()


def cancelar_alerta_telegram(st, alerta=None):
    """Cancela e apaga o alerta/pre-alerta atual do Telegram."""
    alerta = alerta or st.get("alerta_ativo")
    if not alerta:
        return
    alert_id = alerta.get("alert_id")
    if alert_id:
        st.setdefault("telegram_alert_status", {})[alert_id] = "cancelled"
    msg_id = alerta.get("msg_id")
    if msg_id:
        deletar_mensagem_telegram(msg_id)



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
        analise_info = alerta.get("analise", {})
        str_entrada = alerta["str_entrada"]
        alerta_msg_id = alerta.get("msg_id")
        alerta_id_atual = alerta.get("alert_id")

        # A confirmação substitui o alerta: o pré-alerta precisa desaparecer
        # do Telegram no momento em que a entrada é confirmada.
        if alerta_id_atual:
            st.setdefault("telegram_alert_status", {})[alerta_id_atual] = "cancelled"
        if alerta_msg_id:
            deletar_mensagem_telegram(alerta_msg_id)

        cor_direcao = "#10b981" if sinal == "CALL" else "#ef4444"

        # Atualiza a tela ANTES de qualquer operação de rede/banco.
        st["sinal_permanente"] = (
            f"<div style='text-align:center;padding:8px;'>"
            f"<div style='color:#67e8f9;font-size:11px;font-weight:900;letter-spacing:1px;'>🎯 SINAL CONFIRMADO</div>"
            f"<div style='font-size:24px;font-weight:900;color:{cor_direcao};margin:6px 0;'>{ativo} • {sinal}</div>"
            f"<div style='font-size:11px;color:#cbd5e1;'>Probabilidade estimada: <b style='color:#4ade80'>{prob}%</b> • M{tf}</div>"
            f"<div style='font-size:10px;color:#94a3b8;margin-top:4px;'>{est_fmt} • Entrada {str_entrada} • Expiração {str_saida}</div>"
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
                msg_id_confirmacao = enviar_telegram(
                    _msg, auto_delete=None, user_solicitante=_user
                )
                if msg_id_confirmacao:
                    st_local = get_user_state(_user)
                    st_local["ultima_confirmacao_msg_id"] = msg_id_confirmacao
                    st_local["ultima_confirmacao_alert_id"] = alerta_id_atual
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

                    # 🛡️ CONSULTA DO CALENDÁRIO ANTES DA VARREDURA
                    # A consulta é feita uma vez por ciclo, e não uma vez por ativo.
                    # Assim, os ativos realmente bloqueados são retirados da lista de
                    # análise, enquanto todos os demais continuam normalmente.
                    calendario_ok = atualizar_calendario_investing()
                    eventos_calendario = INVESTING_CALENDAR_CACHE.get("events", []) if calendario_ok else []

                    ativos_bloqueados = set()
                    eventos_bloqueados = {}
                    detalhes_bloqueados = []

                    if calendario_ok:
                        for ativo_candidato in ativos:
                            evento_candidato = evento_bloqueia_ativo(ativo_candidato, agora_scan, eventos_calendario)
                            if evento_candidato:
                                ativos_bloqueados.add(ativo_candidato)
                                eventos_bloqueados[ativo_candidato] = evento_candidato
                                dt_evento = evento_candidato.get("datetime")
                                dt_liberacao = (dt_evento + timedelta(minutes=NEWS_LOCK_AFTER_MIN)) if dt_evento else None
                                detalhes_bloqueados.append({
                                    "ativo": ativo_candidato,
                                    "currency": evento_candidato.get("currency", ""),
                                    "impact": int(evento_candidato.get("impact", NEWS_MIN_IMPACT)),
                                    "event": evento_candidato.get("event", "Evento econômico"),
                                    "horario": dt_evento.strftime("%H:%M") if hasattr(dt_evento, "strftime") else "--:--",
                                    "liberacao": dt_liberacao.strftime("%H:%M") if hasattr(dt_liberacao, "strftime") else "--:--",
                                })

                        if ativos_bloqueados:
                            st["news_guard_status"] = f"{len(ativos_bloqueados)} ATIVO(S) BLOQUEADO(S) POR NOTÍCIA"
                            st["news_guard_event"] = {
                                "blocked_assets": sorted(ativos_bloqueados),
                                "count": len(ativos_bloqueados),
                            }
                        else:
                            st["news_guard_status"] = "ATIVA — SEM BLOQUEIO"
                            st["news_guard_event"] = None
                    else:
                        # FAIL-OPEN: se o Investing não responder, não inventamos
                        # uma notícia e não bloqueamos o mercado inteiro.
                        ativos_bloqueados = set()
                        st["news_guard_status"] = "INVESTING INDISPONÍVEL — ANÁLISE LIBERADA"
                        st["news_guard_event"] = None
                        st["news_guard_updated"] = time.time()

                    detalhes_bloqueados.sort(key=lambda x: (x.get("liberacao", "99:99"), x.get("ativo", "")))
                    st["news_blocked_assets"] = detalhes_bloqueados

                    # Somente ativos sem notícia de 2/3 touros entram na lista de análise.
                    ativos_scan = [a for a in ativos if a not in ativos_bloqueados]
                    random.shuffle(ativos_scan)

                    if not ativos_scan:
                        st["ativo_atual"] = "TODOS OS ATIVOS BLOQUEADOS POR NOTÍCIA"
                        st["ultimo_sinal"] = (
                            "<div class='system-console' style='color:#ef4444;'>"
                            "🛡️ <b>VARREDURA TEMPORARIAMENTE PAUSADA</b><br>"
                            "Todos os ativos selecionados estão dentro de uma janela de proteção de notícia 2/3 touros.<br>"
                            "<span style='color:#94a3b8;'>A varredura será retomada automaticamente quando cada janela de 30 minutos terminar.</span>"
                            "</div>"
                        )
                        continue

                    for ativo in ativos_scan:
                        if not st.get("bot_iniciado") or st.get("bot_pausado"):
                            break

                        # O ativo já passou pelo filtro de notícias acima, portanto
                        # ele não aparece na lista de análise enquanto estiver travado.
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
                        melhor_analise = None
                        candidatos = []

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
                            sinal_test, prob_test, analise_test = analisar_estrategia_detalhada(data, est_nome)
                            if sinal_test:
                                candidatos.append({"sinal": sinal_test, "prob": prob_test, "estrategia": est_nome, "analise": analise_test})

                        # Confluência entre estratégias: quando mais de uma estratégia aponta
                        # na mesma direção, adicionamos um pequeno bônus à probabilidade.
                        if candidatos:
                            for cand in candidatos:
                                concordantes = sum(1 for x in candidatos if x["sinal"] == cand["sinal"] and x["estrategia"] != cand["estrategia"])
                                cand["prob"] = min(98, cand["prob"] + min(5, concordantes * 2))
                            melhor = max(candidatos, key=lambda x: x["prob"])
                            sinal_encontrado = melhor["sinal"]
                            est_nome_encontrada = melhor["estrategia"]
                            maior_prob = int(melhor["prob"])
                            melhor_analise = dict(melhor["analise"])
                            melhor_analise["ativo"] = ativo
                            melhor_analise["direcao"] = sinal_encontrado
                            melhor_analise["probabilidade"] = maior_prob
                            melhor_analise["estrategia"] = est_nome_encontrada
                            melhor_analise["estrategia_fmt"] = NOME_ESTRATEGIAS_DISPLAY.get(est_nome_encontrada, est_nome_encontrada)
                            melhor_analise["grafico"] = [float(x) for x in data["close"][-30:]]
                            melhor_analise["motivos"] = melhor_analise.get("confluencias", [])
                            melhor_analise["estrategias_concordantes"] = [NOME_ESTRATEGIAS_DISPLAY.get(x["estrategia"], x["estrategia"]) for x in candidatos if x["sinal"] == sinal_encontrado]
                        else:
                            # Mesmo sem sinal, mostramos o diagnóstico do ativo para o painel.
                            diag = _indicadores_confluencia(data, None)
                            diag["ativo"] = ativo
                            diag["direcao"] = None
                            diag["probabilidade"] = 0
                            diag["estrategia"] = None
                            diag["estrategia_fmt"] = "Sem sinal validado"
                            diag["grafico"] = [float(x) for x in data["close"][-30:]]
                            diag["motivos"] = diag.get("confluencias", [])
                            melhor_analise = diag

                                        # Quando existe um pré-alerta/sinal confirmado, não deixamos a varredura
                        # dos demais ativos substituir a análise do sinal que está em operação.
                        # Isso garante que o painel continue mostrando o MESMO ativo confirmado.
                        if not bloquear_novos_alertas or not st.get("analise_atual"):
                            st["analise_atual"] = melhor_analise

                        # Sinais só avançam quando existe uma probabilidade estimada mínima.
                        if sinal_encontrado and maior_prob >= 80 and not bloquear_novos_alertas:
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
                                        f"🔄 <b>VISION PRO — ALERTA ATUALIZADO</b>\n"
                                        f"━━━━━━━━━━━━━━━━━━━━\n"
                                        f"💱 <b>{ativo}</b> • M{tf}\n"
                                        f"↕️ <b>DIREÇÃO:</b> {sinal_encontrado}\n"
                                        f"🔥 <b>PROBABILIDADE ESTIMADA:</b> {maior_prob}%\n"
                                        f"🧠 <b>Estratégia:</b> {nome_est_formatado}\n"
                                        f"📊 <b>Confluência:</b> {melhor_analise.get('confluencia',0):.0f}/100\n"
                                        f"🤝 <b>Concordância:</b> {len(melhor_analise.get('estrategias_concordantes',[]))} estratégia(s)\n"
                                        f"🕐 <b>Entrada:</b> {str_entrada}\n"
                                        f"━━━━━━━━━━━━━━━━━━━━\n"
                                        f"⚠️ <i>Alerta anterior substituído por uma leitura de maior probabilidade.</i>"
                                    )

                                    # Troca o alerta no painel imediatamente.
                                    st["ultima_confirmacao_msg_id"] = None
                                    st["ultima_confirmacao_alert_id"] = None
                                    st["alerta_ativo"] = {
                                        "ativo": ativo,
                                        "sinal": sinal_encontrado,
                                        "estrategia": est_nome_encontrada,
                                        "estrategia_fmt": nome_est_formatado,
                                        "probabilidade": maior_prob,
                                        "analise": melhor_analise,
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
                                    f"⚠️ <b>VISION PRO — PRÉ-ALERTA</b>\n"
                                    f"━━━━━━━━━━━━━━━━━━━━\n"
                                    f"💱 <b>{ativo}</b> • M{tf}\n"
                                    f"↕️ <b>DIREÇÃO:</b> {sinal_encontrado}\n"
                                    f"🔥 <b>PROBABILIDADE ESTIMADA:</b> {maior_prob}%\n"
                                    f"🧠 <b>Estratégia:</b> {nome_est_formatado}\n"
                                    f"📊 <b>Confluência:</b> {melhor_analise.get('confluencia',0):.0f}/100\n"
                                    f"🤝 <b>Concordância:</b> {len(melhor_analise.get('estrategias_concordantes',[]))} estratégia(s)\n"
                                    f"🕐 <b>Entrada prevista:</b> {str_entrada}\n"
                                    f"━━━━━━━━━━━━━━━━━━━━\n"
                                    f"🛡️ <i>A confirmação final ocorre no horário programado.</i>"
                                )
                                
                                novo_alert_id = str(time.time_ns())

                                st["ultima_confirmacao_msg_id"] = None
                                st["ultima_confirmacao_alert_id"] = None
                                st["alerta_ativo"] = {
                                    "ativo": ativo,
                                    "sinal": sinal_encontrado,
                                    "estrategia": est_nome_encontrada,
                                    "estrategia_fmt": nome_est_formatado,
                                    "probabilidade": maior_prob,
                                    "analise": melhor_analise,
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
