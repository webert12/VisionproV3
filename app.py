import requests
import time
import math
import pytz
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
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
            "ultimo_sinal_id": None,
            "diagnostico": {
                "ciclo_inicio": time.time(), "ciclo_num": 0, "ativos_analisados": 0, "dados_ok": 0,
                "dados_falha": 0, "candidatos": 0, "rejeitados": 0,
                "oportunidades_validadas": 0, "ultima_oportunidade": None,
                "ultimo_motivo": "Aguardando análise...", "motivo_contagem": {},
                "estrategia_contagem": {}, "ultima_atualizacao": time.time(),
                "ultimo_score": 0, "ultimo_direcao": None, "estrategias_concordantes": 0,
                "estrategias_analisadas": 0, "ultimo_ativo_analisado": None,
                "ultimo_setup": None, "ultimo_detalhe": "Nenhum ativo analisado ainda.",
                "ultimo_ciclo_segundos": 0.0, "ativos_por_ciclo": 0
            }
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

        .diagnostic-box { background: rgba(15,23,42,.78); border: 1px solid rgba(0,242,254,.18); border-radius: 12px; padding: 12px; margin-bottom: 12px; font-size: 12px; }
        .diag-grid { display:grid; grid-template-columns:repeat(3,1fr); gap:8px; margin:8px 0; }
        .diag-item { background:rgba(255,255,255,.03); border-radius:8px; padding:8px; text-align:center; }
        .diag-label { color:#94a3b8; font-size:10px; text-transform:uppercase; }
        .diag-value { color:#e2e8f0; font-weight:800; font-size:14px; margin-top:2px; }
        .diag-reason { color:#f59e0b; margin-top:8px; line-height:1.45; }
        .diag-bar { height:6px; background:#1e293b; border-radius:8px; overflow:hidden; margin-top:7px; }
        .diag-fill { height:100%; background:#00f2fe; transition:width .3s; }

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

        <div class="diagnostic-box" id="diagnostic-box">
            <b style="color:#00f2fe;">🔎 DIAGNÓSTICO DA VARREDURA</b>
            <div class="diag-grid">
                <div class="diag-item"><div class="diag-label">Ativos</div><div class="diag-value" id="diag-assets">0</div></div>
                <div class="diag-item"><div class="diag-label">Oportunidades</div><div class="diag-value" id="diag-candidates">0</div></div>
                <div class="diag-item"><div class="diag-label">Rejeitadas</div><div class="diag-value" id="diag-rejected">0</div></div>
            </div>
            <div>Score da última oportunidade: <b id="diag-score">0/100</b></div>
            <div class="diag-bar"><div class="diag-fill" id="diag-fill" style="width:0%"></div></div>
            <div class="diag-reason" id="diag-reason">Aguardando análise...</div>
            <div style="color:#cbd5e1;margin-top:7px;line-height:1.5;" id="diag-detail">Nenhum ativo analisado ainda.</div>
            <div style="color:#64748b;margin-top:7px;line-height:1.5;" id="diag-cycle">Ciclo: — | Velocidade: — | Dados reais: —</div>
            <div style="color:#64748b;margin-top:5px;line-height:1.5;" id="diag-strategies">Estratégias: —</div>
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
                            <option value="CONFLUENCIA_PRICE_ACTION" {% if estrat == 'CONFLUENCIA_PRICE_ACTION' %}selected{% endif %}>Confluência Price Action</option>
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
            <button onclick="location.href='/backtest'" style="width:100%; margin-top:10px; padding:12px; background:rgba(139,92,246,0.12); border:1px solid #8b5cf6; color:#c4b5fd; font-weight:bold; border-radius:10px; cursor:pointer;">🧪 ABRIR LABORATÓRIO DE BACKTEST</button>

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

        function sendCommand(cmd) {
            fetch('/command/' + cmd).then(r => r.json()).then(data => {
                if(data.redirect) window.location.href = data.redirect;
            });
        }

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
                const d = data.diagnostico || {};
                if(document.getElementById('diag-assets')) document.getElementById('diag-assets').innerText = d.ativos_analisados || 0;
                if(document.getElementById('diag-candidates')) document.getElementById('diag-candidates').innerText = d.candidatos || 0;
                if(document.getElementById('diag-rejected')) document.getElementById('diag-rejected').innerText = d.rejeitados || 0;
                if(document.getElementById('diag-score')) document.getElementById('diag-score').innerText = (d.ultimo_score || 0) + '/100';
                if(document.getElementById('diag-fill')) document.getElementById('diag-fill').style.width = Math.min(100, d.ultimo_score || 0) + '%';
                if(document.getElementById('diag-reason')) document.getElementById('diag-reason').innerText = 'Status: ' + (d.ultimo_motivo || 'Aguardando análise...');
                if(document.getElementById('diag-detail')) document.getElementById('diag-detail').innerText = (d.ultimo_ativo_analisado || '—') + ' | ' + (d.ultimo_detalhe || 'Sem detalhe') + (d.ultimo_setup ? ' | Setup: ' + d.ultimo_setup : '');
                 if(document.getElementById('diag-cycle')) document.getElementById('diag-cycle').innerText = 'Ciclo: ' + (d.ciclo_num || 0) + ' | Duração: ' + Number(d.ultimo_ciclo_segundos || 0).toFixed(1) + 's | Dados reais: ' + (d.dados_ok || 0) + '/' + (d.ativos_analisados || 0);
                 if(document.getElementById('diag-strategies')) { const sc=d.estrategia_contagem||{}; const parts=Object.entries(sc).map(([k,v]) => k.replace('CONFLUENCIA_PRICE_ACTION','PRICE ACTION').replace('LOGICA_DO_PRECO','LÓGICA').replace('RSI_MACD_MA','RSI/MACD').replace('MHI1','MHI1').replace('REVERSAO','REVERSÃO')+': '+v); document.getElementById('diag-strategies').innerText='Estratégias com candidato: '+(parts.join(' | ')||'nenhuma'); }
                
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
                // Nova consulta 250ms após a resposta, sem acumular requisições.
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

            CREATE TABLE IF NOT EXISTS backtest_execucoes (
                id SERIAL PRIMARY KEY,
                user_email VARCHAR(255) NOT NULL,
                ativo VARCHAR(80) NOT NULL,
                timeframe INT NOT NULL,
                executado_em TIMESTAMP NOT NULL,
                amostras INT DEFAULT 0,
                sinais INT DEFAULT 0,
                acertos INT DEFAULT 0,
                erros INT DEFAULT 0,
                neutros INT DEFAULT 0,
                taxa FLOAT DEFAULT 0.0
            );

            ALTER TABLE historico_sinais ADD COLUMN IF NOT EXISTS ativo VARCHAR(80);
            ALTER TABLE historico_sinais ADD COLUMN IF NOT EXISTS timeframe INT;
            ALTER TABLE historico_sinais ADD COLUMN IF NOT EXISTS estrategia VARCHAR(100);
            ALTER TABLE historico_sinais ADD COLUMN IF NOT EXISTS direcao VARCHAR(10);
            ALTER TABLE historico_sinais ADD COLUMN IF NOT EXISTS score INT DEFAULT 0;
            ALTER TABLE historico_sinais ADD COLUMN IF NOT EXISTS setup VARCHAR(120);
            ALTER TABLE historico_sinais ADD COLUMN IF NOT EXISTS analise_json TEXT;
            ALTER TABLE historico_sinais ADD COLUMN IF NOT EXISTS entrada_em TIMESTAMP;

            CREATE TABLE IF NOT EXISTS estatisticas_setups (
                id SERIAL PRIMARY KEY,
                chave VARCHAR(300) UNIQUE NOT NULL,
                ativo VARCHAR(80) NOT NULL,
                timeframe INT NOT NULL,
                estrategia VARCHAR(100) NOT NULL,
                setup VARCHAR(120) NOT NULL,
                direcao VARCHAR(10) NOT NULL,
                sinais INT DEFAULT 0, wins INT DEFAULT 0, reds INT DEFAULT 0,
                atualizado_em TIMESTAMP NOT NULL
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

def registrar_sinal_bd(email, sinal_str, metadata=None):
    try:
        metadata=metadata or {}; conn=get_db_connection(); cur=conn.cursor()
        cur.execute("""INSERT INTO historico_sinais
        (user_email,sinal,resultado,ativo,timeframe,estrategia,direcao,score,setup,analise_json,entrada_em)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id;""",
        (email.strip().lower(),sinal_str,'Analisando...',metadata.get('ativo'),metadata.get('timeframe'),metadata.get('estrategia'),
         metadata.get('direcao'),int(metadata.get('score') or 0),metadata.get('setup'),json.dumps(metadata.get('analise') or {},ensure_ascii=False,default=str),
         metadata.get('entrada_em') or agora_brasilia().replace(tzinfo=None)))
        row=cur.fetchone(); conn.commit(); cur.close(); conn.close(); return row[0] if row else None
    except Exception as e:
        print(f'⚠️ Erro ao registrar sinal V4: {e}'); return None

def buscar_historico_bd(email):
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("""
            SELECT id, sinal, resultado, ativo, timeframe, estrategia, direcao, score, setup
            FROM historico_sinais 
            WHERE user_email = %s 
            ORDER BY id DESC LIMIT 20;
        """, (email.strip().lower(),))
        res = cur.fetchall()
        cur.close()
        conn.close()
        return [{"id": r["id"], "sinal": r["sinal"], "res": r["resultado"], "ativo": r.get("ativo"), "timeframe": r.get("timeframe"), "estrategia": r.get("estrategia"), "direcao": r.get("direcao"), "score": r.get("score") or 0, "setup": r.get("setup")} for r in res]
    except Exception:
        return []

def atualizar_ultimo_sinal_bd(email, resultado):
    try:
        email_clean = email.strip().lower()
        st = get_user_state(email_clean)
        sinal_id = st.get("ultimo_sinal_id")
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        if sinal_id:
            cur.execute("UPDATE historico_sinais SET resultado = %s WHERE id = %s AND user_email = %s RETURNING id;", (resultado, sinal_id, email_clean))
            res = cur.fetchone()
        else:
            cur.execute("SELECT id FROM historico_sinais WHERE user_email = %s ORDER BY id DESC LIMIT 1;", (email_clean,))
            res = cur.fetchone()
            if res:
                cur.execute("UPDATE historico_sinais SET resultado = %s WHERE id = %s;", (resultado, res["id"]))
        conn.commit(); cur.close(); conn.close()
        if sinal_id:
            registrar_resultado_setup(sinal_id, resultado)
        st["ultimo_sinal_id"] = None
    except Exception:
        pass

# ================= V5: MEMÓRIA ESTATÍSTICA E SETUPS =================
def _classificar_setup(details):
    bo=(details or {}).get('breakout',{}) or {}; kind=bo.get('kind'); trend=(details or {}).get('trend','NEUTRA')
    reasons=' '.join((details or {}).get('reasons',[])).lower()
    if kind=='FAKEOUT': return 'FAKEOUT_REVERSAO'
    if kind=='BREAKOUT' and bo.get('retest'): return 'BREAKOUT_RETESTE'
    if kind=='BREAKOUT': return 'BREAKOUT_CONTINUACAO'
    if 'exaustão' in reasons: return 'EXAUSTAO_REVERSAO'
    if 'engolfo' in reasons and 'suporte' in reasons: return 'ENGOLFO_SUPORTE'
    if 'engolfo' in reasons and 'resistência' in reasons: return 'ENGOLFO_RESISTENCIA'
    if 'rejeição inferior' in reasons: return 'REJEICAO_SUPORTE'
    if 'rejeição superior' in reasons: return 'REJEICAO_RESISTENCIA'
    if trend.startswith('ALTA'): return 'CONTINUACAO_ALTA'
    if trend.startswith('BAIXA'): return 'CONTINUACAO_BAIXA'
    return 'PRICE_ACTION_NEUTRO'

def _setup_key(ativo,tf,estrategia,setup,direcao):
    return f'{ativo}|M{int(tf)}|{estrategia}|{setup}|{direcao}'

_ADAPTIVE_CACHE={}; _ADAPTIVE_CACHE_LOCK=threading.Lock()

def obter_estatistica_setup(ativo,tf,estrategia,setup,direcao,ttl=60):
    key=_setup_key(ativo,tf,estrategia,setup,direcao); now=time.time()
    with _ADAPTIVE_CACHE_LOCK:
        cached=_ADAPTIVE_CACHE.get(key)
        if cached and now-cached['time']<ttl: return cached['data']
    data={'sinais':0,'wins':0,'reds':0,'winrate':None,'ajuste':0}
    try:
        conn=get_db_connection(); cur=conn.cursor(cursor_factory=RealDictCursor)
        cur.execute('SELECT sinais,wins,reds FROM estatisticas_setups WHERE chave=%s',(key,)); row=cur.fetchone(); cur.close(); conn.close()
        if row:
            data['sinais']=int(row.get('sinais') or 0); data['wins']=int(row.get('wins') or 0); data['reds']=int(row.get('reds') or 0)
            d=data['wins']+data['reds']
            if d:
                data['winrate']=round(data['wins']/d*100,2)
                if d>=20: data['ajuste']=max(-8,min(8,round((data['winrate']-50)*0.16)))
    except Exception as e: print(f'⚠️ Estatística de setup indisponível: {e}')
    with _ADAPTIVE_CACHE_LOCK: _ADAPTIVE_CACHE[key]={'time':now,'data':data}
    return data

def registrar_resultado_setup(signal_id,resultado):
    if resultado not in ('Win','WinG1','Red'): return
    try:
        conn=get_db_connection(); cur=conn.cursor(cursor_factory=RealDictCursor)
        cur.execute('SELECT ativo,timeframe,estrategia,direcao,setup FROM historico_sinais WHERE id=%s',(signal_id,)); row=cur.fetchone()
        if not row or not all(row.get(k) for k in ('ativo','timeframe','estrategia','direcao','setup')):
            cur.close(); conn.close(); return
        key=_setup_key(row['ativo'],row['timeframe'],row['estrategia'],row['setup'],row['direcao'])
        cur.execute("""INSERT INTO estatisticas_setups (chave,ativo,timeframe,estrategia,setup,direcao,sinais,wins,reds,atualizado_em)
                       VALUES (%s,%s,%s,%s,%s,%s,1,%s,%s,%s)
                       ON CONFLICT (chave) DO UPDATE SET
                         sinais=estatisticas_setups.sinais+1, wins=estatisticas_setups.wins+EXCLUDED.wins,
                         reds=estatisticas_setups.reds+EXCLUDED.reds, atualizado_em=EXCLUDED.atualizado_em""",
                    (key,row['ativo'],row['timeframe'],row['estrategia'],row['setup'],row['direcao'],1 if resultado in ('Win','WinG1') else 0,1 if resultado=='Red' else 0,agora_brasilia().replace(tzinfo=None)))
        conn.commit(); cur.close(); conn.close()
        with _ADAPTIVE_CACHE_LOCK: _ADAPTIVE_CACHE.pop(key,None)
    except Exception as e: print(f'⚠️ Erro ao atualizar estatística do setup: {e}')

def _enriquecer_score_adaptativo(ativo,tf,estrategia,direcao,score,details):
    setup=_classificar_setup(details); hist=obter_estatistica_setup(ativo,tf,estrategia,setup,direcao)
    details=dict(details or {}); details['setup']=setup; details['historico_setup']=hist
    return max(0,min(100,int(round(score+hist.get('ajuste',0))))),details

# ================= BOT CONFIGS & ESTRATÉGIAS =================
LISTA_ESTRATEGIAS = ["CONFLUENCIA_PRICE_ACTION", "LOGICA_DO_PRECO", "RSI_MACD_MA", "MHI1", "REVERSAO"]

NOME_ESTRATEGIAS_DISPLAY = {
    "CONFLUENCIA_PRICE_ACTION": "Confluência Price Action",
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
def get_data_v2(ticker, tf, velas_minimas=60):
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
        
        # IMPORTANTE: nunca gerar candles artificiais. Um sinal deve ser baseado
        # exclusivamente em dados reais; se a fonte falhar, aguardamos novos dados.
        return None

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

# ================= MOTOR V2: PRICE ACTION + ESTRUTURA + CONTEXTO =================
# O motor V2 usa SCORE DE CONFLUÊNCIA (0-100), e não "probabilidade".
# Nenhum score representa taxa de acerto estatística. Isso só pode ser medido
# posteriormente com backtest/out-of-sample em dados reais.

def _safe_div(a, b, default=0.0):
    try:
        b = float(b)
        return float(a) / b if abs(b) > 1e-12 else default
    except Exception:
        return default

def _rolling_mean(arr, n):
    arr = np.asarray(arr, dtype=float)
    if len(arr) == 0:
        return 0.0
    return float(np.mean(arr[-n:]))

def _atr_series(data, period=14):
    h, l, c = map(lambda x: np.asarray(x, dtype=float), (data['high'], data['low'], data['close']))
    if len(c) < 2:
        return np.zeros(len(c))
    tr = np.empty(len(c)); tr[0] = h[0] - l[0]
    tr[1:] = np.maximum(h[1:] - l[1:], np.maximum(np.abs(h[1:] - c[:-1]), np.abs(l[1:] - c[:-1])))
    out = np.zeros(len(c)); out[0] = tr[0]
    alpha = 1.0 / max(1, period)
    for j in range(1, len(c)):
        out[j] = alpha * tr[j] + (1-alpha) * out[j-1]
    return out

def _atr(data, period=14):
    a = _atr_series(data, period)
    return float(a[-1]) if len(a) else 0.0

def _ema_last(c, period):
    return float(calcular_ema(np.asarray(c, dtype=float), period)[-1])

def _rsi_series(c, period=14):
    c = np.asarray(c, dtype=float)
    if len(c) < period + 1:
        return np.full(len(c), 50.0)
    d = np.diff(c)
    gain = np.maximum(d, 0.0); loss = np.maximum(-d, 0.0)
    avg_g = np.mean(gain[:period]); avg_l = np.mean(loss[:period])
    r = np.full(len(c), 50.0); r[period] = 100.0 if avg_l == 0 else 100 - 100/(1+avg_g/avg_l)
    for j in range(period+1, len(c)):
        avg_g = (avg_g*(period-1) + gain[j-1]) / period
        avg_l = (avg_l*(period-1) + loss[j-1]) / period
        r[j] = 100.0 if avg_l == 0 else 100 - 100/(1+avg_g/avg_l)
    return r

def _macd(c):
    c = np.asarray(c, dtype=float)
    e12, e26 = calcular_ema(c, 12), calcular_ema(c, 26)
    line = e12 - e26
    sig = calcular_ema(line, 9)
    return line, sig, line - sig

def _pivot_points(h, l, lookback=2):
    h, l = np.asarray(h), np.asarray(l)
    highs, lows = [], []
    for k in range(lookback, len(h)-lookback):
        if h[k] == np.max(h[k-lookback:k+lookback+1]): highs.append((k, float(h[k])))
        if l[k] == np.min(l[k-lookback:k+lookback+1]): lows.append((k, float(l[k])))
    return highs, lows

def _market_structure(data):
    h, l, c = data['high'], data['low'], data['close']
    ph, pl = _pivot_points(h, l, 2)
    if len(ph) < 2 or len(pl) < 2: return 'NEUTRA', 0, {}
    last_h, prev_h = ph[-1][1], ph[-2][1]
    last_l, prev_l = pl[-1][1], pl[-2][1]
    hh, hl = last_h > prev_h, last_l > prev_l
    lh, ll = last_h < prev_h, last_l < prev_l
    ema20, ema50 = _ema_last(c, 20), _ema_last(c, min(50, len(c)))
    if hh and hl: return 'ALTA', 18, {'hh':True,'hl':True,'lh':False,'ll':False}
    if lh and ll: return 'BAIXA', 18, {'hh':False,'hl':False,'lh':True,'ll':True}
    # Estrutura mista + alinhamento das médias = tendência mais fraca.
    if ema20 > ema50 and c[-1] > ema20: return 'ALTA_FRACA', 8, {'hh':hh,'hl':hl,'lh':lh,'ll':ll}
    if ema20 < ema50 and c[-1] < ema20: return 'BAIXA_FRACA', 8, {'hh':hh,'hl':hl,'lh':lh,'ll':ll}
    return 'NEUTRA', 0, {'hh':hh,'hl':hl,'lh':lh,'ll':ll}

def _support_resistance(data, tolerance_atr=0.45):
    h, l, c = map(lambda x: np.asarray(x, dtype=float), (data['high'], data['low'], data['close']))
    atr = _atr(data)
    if atr <= 0 or len(c) < 20: return None, None, atr, {}
    ph, pl = _pivot_points(h, l, 2)
    price = float(c[-1])
    highs = [x[1] for x in ph[-8:]]
    lows = [x[1] for x in pl[-8:]]
    # Agrupa níveis próximos e prefere zonas testadas várias vezes.
    def cluster(vals):
        if not vals: return []
        groups=[]
        for v in sorted(vals):
            if not groups or abs(v-np.mean(groups[-1])) > atr*0.45: groups.append([v])
            else: groups[-1].append(v)
        return [(float(np.mean(g)), len(g)) for g in groups]
    res = [(v,n) for v,n in cluster(highs) if v > price]
    sup = [(v,n) for v,n in cluster(lows) if v < price]
    resistance = min(res, key=lambda x:x[0]-price) if res else None
    support = max(sup, key=lambda x:price-x[0]) if sup else None
    near_s = support and abs(price-support[0]) <= atr*tolerance_atr
    near_r = resistance and abs(resistance[0]-price) <= atr*tolerance_atr
    details={'support_tests':support[1] if near_s else 0,'resistance_tests':resistance[1] if near_r else 0}
    return support[0] if near_s else None, resistance[0] if near_r else None, atr, details

def _candle_features(data, idx=-1):
    o,h,l,c = [float(data[k][idx]) for k in ('open','high','low','close')]
    rng=max(h-l,1e-12); body=abs(c-o); upper=h-max(o,c); lower=min(o,c)-l
    return {'bull':c>o,'bear':c<o,'range':rng,'body':body,'body_ratio':body/rng,
            'upper_ratio':upper/rng,'lower_ratio':lower/rng,'close_pos':(c-l)/rng}

def _price_action(data):
    if len(data['close']) < 5: return None,0,[]
    o,h,l,c=data['open'],data['high'],data['low'],data['close']; f=_candle_features(data); reasons=[]
    bull=bear=0
    # Rejeição contextualizada: pavio grande + fechamento favorável.
    if f['lower_ratio'] >= .40 and f['close_pos'] >= .62: bull += 14; reasons.append('rejeição inferior')
    if f['upper_ratio'] >= .40 and f['close_pos'] <= .38: bear += 14; reasons.append('rejeição superior')
    # Engolfo real, exigindo corpo relevante.
    pf=_candle_features(data,-2)
    if f['bull'] and pf['bear'] and c[-1] >= o[-2] and o[-1] <= c[-2] and f['body_ratio'] >= .45:
        bull += 15; reasons.append('engolfo comprador')
    if f['bear'] and pf['bull'] and c[-1] <= o[-2] and o[-1] >= c[-2] and f['body_ratio'] >= .45:
        bear += 15; reasons.append('engolfo vendedor')
    # Impulso relativo ao histórico recente.
    avg=_rolling_mean(h[-11:-1]-l[-11:-1],10)
    if avg>0 and f['range'] >= 1.5*avg and f['body_ratio'] >= .65:
        if f['bull']: bull += 8; reasons.append('candle de impulso comprador')
        elif f['bear']: bear += 8; reasons.append('candle de impulso vendedor')
    # Inside/outside bar como contexto, não sinal isolado.
    if h[-1] < h[-2] and l[-1] > l[-2]: reasons.append('inside bar')
    if h[-1] > h[-2] and l[-1] < l[-2]: reasons.append('outside bar')
    if bull>bear and bull>=12: return 'CALL',min(25,bull),reasons
    if bear>bull and bear>=12: return 'PUT',min(25,bear),reasons
    return None,0,reasons

def _breakout_state(data):
    h,l,o,c=data['high'],data['low'],data['open'],data['close']; atr=_atr(data)
    if len(c)<25 or atr<=0: return None,0,[],{'kind':None}
    # Usa uma janela que exclui a vela atual para evitar contaminar o nível.
    resistance=float(np.max(h[-21:-1])); support=float(np.min(l[-21:-1])); f=_candle_features(data); reasons=[]
    body_ratio=f['body_ratio']; close=c[-1]
    # Rompimento confirmado: corpo + fechamento além do nível.
    if close > resistance + .12*atr and body_ratio >= .55:
        reasons.append('rompimento de resistência confirmado');
        # Reteste recente da região aumenta a qualidade.
        retest=bool(np.min(l[-4:-1]) <= resistance + .25*atr)
        if retest: reasons.append('reteste de resistência')
        return 'CALL',24 if retest else 18,reasons,{'kind':'BREAKOUT','level':resistance,'retest':retest}
    if close < support - .12*atr and body_ratio >= .55:
        reasons.append('rompimento de suporte confirmado')
        retest=bool(np.max(h[-4:-1]) >= support - .25*atr)
        if retest: reasons.append('reteste de suporte')
        return 'PUT',24 if retest else 18,reasons,{'kind':'BREAKOUT','level':support,'retest':retest}
    # Falso rompimento / sweep: atravessa e fecha de volta para dentro.
    if h[-1] > resistance and close < resistance:
        reasons.append('falso rompimento de resistência / sweep'); return 'PUT',22,reasons,{'kind':'FAKEOUT','level':resistance}
    if l[-1] < support and close > support:
        reasons.append('falso rompimento de suporte / sweep'); return 'CALL',22,reasons,{'kind':'FAKEOUT','level':support}
    return None,0,reasons,{'kind':None}

def _exhaustion_state(data):
    h,l,o,c=data['high'],data['low'],data['open'],data['close']; atr=_atr(data)
    if len(c)<12 or atr<=0: return None,0,[]
    f=_candle_features(data); avg=_rolling_mean(h[-11:-1]-l[-11:-1],10); reasons=[]
    up=sum(1 for j in range(1,6) if c[-j]>o[-j]); dn=sum(1 for j in range(1,6) if c[-j]<o[-j])
    # Exaustão exige sequência + expansão + perda de fechamento no extremo.
    if up>=4 and f['range']>=max(1.35*avg,1.45*atr) and f['upper_ratio']>=.25 and f['close_pos']<.72:
        reasons.append('exaustão compradora'); return 'PUT',17,reasons
    if dn>=4 and f['range']>=max(1.35*avg,1.45*atr) and f['lower_ratio']>=.25 and f['close_pos']>.28:
        reasons.append('exaustão vendedora'); return 'CALL',17,reasons
    return None,0,reasons

def _momentum_context(data):
    c=np.asarray(data['close'],dtype=float); rsi=float(_rsi_series(c)[-1]); macd,sig,hist=_macd(c)
    e9,e20,e50=_ema_last(c,9),_ema_last(c,20),_ema_last(c,min(50,len(c)))
    out={'rsi':rsi,'macd_hist':float(hist[-1]),'ema9':e9,'ema20':e20,'ema50':e50}
    call=put=0; reasons=[]
    if e9>e20>e50 and c[-1]>e9: call+=12; reasons.append('alinhamento de tendência pelas EMAs')
    elif e9<e20<e50 and c[-1]<e9: put+=12; reasons.append('alinhamento de tendência pelas EMAs')
    if hist[-1]>0 and hist[-1]>=hist[-2]: call+=7; reasons.append('MACD com momentum comprador')
    elif hist[-1]<0 and hist[-1]<=hist[-2]: put+=7; reasons.append('MACD com momentum vendedor')
    if 42<=rsi<=68 and hist[-1]>0: call+=5; reasons.append('RSI em zona saudável para alta')
    if 32<=rsi<=58 and hist[-1]<0: put+=5; reasons.append('RSI em zona saudável para baixa')
    # Extremos de RSI não geram entrada sozinhos; servem como alerta de extensão.
    if rsi>=78: out['overbought']=True; reasons.append('RSI muito esticado')
    elif rsi<=22: out['oversold']=True; reasons.append('RSI muito esticado')
    return ('CALL',call,reasons,out) if call>put else ('PUT',put,reasons,out) if put>call else (None,0,reasons,out)

def _advanced_confluence(data):
    if len(data['close']) < 40: return None,0,{'reasons':[],'evidence':0}
    trend,tpts,structure=_market_structure(data)
    pa_sig,pa_pts,pa_reasons=_price_action(data)
    bo_sig,bo_pts,bo_reasons,bo_info=_breakout_state(data)
    ex_sig,ex_pts,ex_reasons=_exhaustion_state(data)
    mom_sig,mom_pts,mom_reasons,mom=_momentum_context(data)
    support,resistance,atr,sr=_support_resistance(data)
    scores={'CALL':0,'PUT':0}; evidence={'CALL':set(),'PUT':set()}; reasons=[]
    def add(sig,pts,tag):
        if sig in scores and pts:
            scores[sig]+=pts; evidence[sig].add(tag)
    if trend.startswith('ALTA'): add('CALL',tpts,'estrutura')
    if trend.startswith('BAIXA'): add('PUT',tpts,'estrutura')
    add(pa_sig,pa_pts,'price_action'); add(bo_sig,bo_pts,'rompimento'); add(ex_sig,ex_pts,'exaustao'); add(mom_sig,mom_pts,'momentum')
    if support is not None: add('CALL',14,'suporte'); reasons.append('preço em zona de suporte')
    if resistance is not None: add('PUT',14,'resistencia'); reasons.append('preço em zona de resistência')
    reasons += pa_reasons + bo_reasons + ex_reasons + mom_reasons
    # Contradições importantes reduzem score.
    if ex_sig=='PUT': scores['CALL']-=12; evidence['CALL'].discard('price_action') if pa_sig=='CALL' and 'price_action' in evidence['CALL'] else None
    if ex_sig=='CALL': scores['PUT']-=12
    if bo_info.get('kind')=='FAKEOUT':
        opposite='CALL' if bo_sig=='PUT' else 'PUT'
        scores[opposite]-=10
    # Volatilidade: evita extremos; não cria sinal.
    atr_series=_atr_series(data); atr_now=float(atr_series[-1]); atr_base=_rolling_mean(atr_series[-25:-1],20)
    volatility='NORMAL'
    if atr_base>0:
        if atr_now>1.8*atr_base: volatility='EXTREMA'
        elif atr_now<0.55*atr_base: volatility='BAIXA'
    if volatility=='EXTREMA':
        scores['CALL']-=10; scores['PUT']-=10; reasons.append('volatilidade extrema: filtro de risco')
    elif volatility=='BAIXA':
        scores['CALL']-=4; scores['PUT']-=4; reasons.append('volatilidade muito baixa')
    # Resistência/suporte muito próximos podem invalidar continuação.
    price=float(data['close'][-1])
    if resistance and bo_sig!='CALL' and resistance-price < atr*0.55:
        scores['CALL']-=10; reasons.append('resistência próxima')
    if support and bo_sig!='PUT' and price-support < atr*0.55:
        scores['PUT']-=10; reasons.append('suporte próximo')
    signal=max(scores,key=scores.get); score=max(0,min(100,int(scores[signal])))
    # Exige 2 fontes independentes e score mínimo. Para breakout/fakeout,
    # o próprio evento conta como uma fonte, mas precisa de contexto adicional.
    ev=len(evidence[signal])
    if bo_info.get('kind') in ('BREAKOUT','FAKEOUT') and ev>=2: pass
    valid=(score>=62 and ev>=2)
    # Impede reversão contra estrutura forte sem Price Action ou fakeout.
    if valid and signal=='CALL' and trend=='BAIXA' and not (pa_sig=='CALL' or bo_info.get('kind')=='FAKEOUT' or ex_sig=='CALL'):
        valid=False; reasons.append('contra estrutura de baixa sem confirmação de reversão')
    if valid and signal=='PUT' and trend=='ALTA' and not (pa_sig=='PUT' or bo_info.get('kind')=='FAKEOUT' or ex_sig=='PUT'):
        valid=False; reasons.append('contra estrutura de alta sem confirmação de reversão')
    if not valid: signal=None; score=0
    details={'trend':trend,'structure':structure,'support':support,'resistance':resistance,'atr':atr,
             'volatility':volatility,'rsi':mom['rsi'],'macd_hist':mom['macd_hist'],
             'score_call':max(0,int(scores['CALL'])),'score_put':max(0,int(scores['PUT'])),
             'evidence':ev,'reasons':list(dict.fromkeys(reasons))[:10], 'breakout':bo_info}
    return signal,score,details

def _strategy_score(data, estrategia):
    c,o,h,l=data['close'],data['open'],data['high'],data['low']
    if len(c)<40: return None,0
    trend,_,_= _market_structure(data)
    pa_sig,pa_pts,_=_price_action(data)
    bo_sig,bo_pts,_,bo_info=_breakout_state(data)
    ex_sig,ex_pts,_=_exhaustion_state(data)
    mom_sig,mom_pts,_,mom=_momentum_context(data)
    signal=None; raw=0
    if estrategia=='LOGICA_DO_PRECO':
        # Preço 2.0: prioriza rejeição/engolfo/estrutura e evita candle isolado.
        candidates=[(pa_sig,pa_pts),(bo_sig,bo_pts),(ex_sig,ex_pts)]
        best=max(candidates,key=lambda x:x[1])
        if best[0]: signal,raw=best
        if signal=='CALL' and trend.startswith('ALTA'): raw+=15
        if signal=='PUT' and trend.startswith('BAIXA'): raw+=15
        if signal=='CALL' and trend=='BAIXA' and bo_info.get('kind')!='FAKEOUT': raw-=12
        if signal=='PUT' and trend=='ALTA' and bo_info.get('kind')!='FAKEOUT': raw-=12
    elif estrategia=='RSI_MACD_MA':
        # Mantém a estratégia, mas exige alinhamento de momentum, evitando RSI extremo isolado.
        if mom_sig: signal=mom_sig; raw=mom_pts+25
        rsi=mom['rsi']
        if signal=='CALL' and 35<=rsi<=65: raw+=8
        if signal=='PUT' and 35<=rsi<=65: raw+=8
    elif estrategia=='MHI1':
        # MHI é tratado como padrão de curto prazo, com filtro estrutural e candle neutro proibido.
        colors=[]
        for j in range(-3,0): colors.append('G' if c[j]>o[j] else 'R' if c[j]<o[j] else 'D')
        if 'D' not in colors:
            if colors.count('G')>=2: signal='PUT'; raw=58
            elif colors.count('R')>=2: signal='CALL'; raw=58
            if signal=='CALL' and trend.startswith('ALTA'): raw+=12
            if signal=='PUT' and trend.startswith('BAIXA'): raw+=12
            # Sequência 3 contra a tendência é melhor tratada como possível correção, não reversão automática.
            if len(set(colors))==1: raw-=8
    elif estrategia in ('REVERSAO','RETRACAO'):
        mid=np.mean(c[-20:]); std=np.std(c[-20:]);
        if std>0:
            upper=mid+2*std; lower=mid-2*std
            if c[-1]<=lower and pa_sig=='CALL': signal='CALL'; raw=68
            elif c[-1]>=upper and pa_sig=='PUT': signal='PUT'; raw=68
            elif c[-1]<=lower and ex_sig=='CALL': signal='CALL'; raw=62
            elif c[-1]>=upper and ex_sig=='PUT': signal='PUT'; raw=62
            if signal=='CALL' and trend=='BAIXA' and bo_info.get('kind')!='FAKEOUT': raw-=10
            if signal=='PUT' and trend=='ALTA' and bo_info.get('kind')!='FAKEOUT': raw-=10
    elif estrategia=='CONFLUENCIA_PRICE_ACTION':
        return _advanced_confluence(data)[:2]
    if not signal or raw<60: return None,0
    # Compatibilidade com o pipeline existente: score, não probabilidade.
    return signal,min(98,int(raw))

def analisar_estrategia(data, estrategia, i=-1):
    return _strategy_score(data, estrategia)

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

# ================= LABORATÓRIO DE BACKTEST V3 =================
def _directional_outcome(data, idx, signal):
    """Resultado mais próximo da operação real: entrada na abertura da próxima vela e expiração no fechamento dela."""
    if idx + 1 >= len(data['close']) or not signal:
        return 'NEUTRO'
    entry = float(data['open'][idx + 1])
    expiry = float(data['close'][idx + 1])
    if abs(expiry - entry) <= max(abs(entry) * 1e-8, 1e-12):
        return 'NEUTRO'
    if signal == 'CALL':
        return 'WIN' if expiry > entry else 'RED'
    return 'WIN' if expiry < entry else 'RED'

def _backtest_strategy(data, estrategia, min_score=60, start=None):
    n = len(data['close'])
    start = max(60, start or 60)
    rows=[]
    for idx in range(start, n-1):
        prefix={k: np.asarray(v[:idx+1]).copy() for k,v in data.items()}
        try:
            sig, score = analisar_estrategia(prefix, estrategia)
            adv_sig, adv_score, adv_details = _advanced_confluence(prefix)
        except Exception:
            continue
        if not sig or score < min_score:
            continue
        # O teste reproduz o filtro estrutural usado pelo robô.
        if adv_sig != sig or adv_score < 55:
            continue
        final_score=int(round((float(score)+float(adv_score))/2.0))
        setup=_classificar_setup(adv_details) if adv_details else 'SEM_SETUP'
        outcome=_directional_outcome(data, idx, sig)
        rows.append({'idx':idx,'signal':sig,'score':final_score,'outcome':outcome,'setup':setup})
    return rows

def _resumo_backtest(rows):
    wins=sum(r['outcome']=='WIN' for r in rows)
    reds=sum(r['outcome']=='RED' for r in rows)
    neutros=sum(r['outcome']=='NEUTRO' for r in rows)
    decididos=wins+reds
    taxa=(wins/decididos*100) if decididos else 0.0
    setups={}
    for r in rows:
        k=r.get('setup','SEM_SETUP'); x=setups.setdefault(k,{'sinais':0,'wins':0,'reds':0})
        x['sinais']+=1; x['wins']+=int(r['outcome']=='WIN'); x['reds']+=int(r['outcome']=='RED')
    for x in setups.values():
        d=x['wins']+x['reds']; x['taxa']=round(x['wins']/d*100,2) if d else 0.0
    return {'sinais':len(rows),'wins':wins,'reds':reds,'neutros':neutros,'taxa':round(taxa,2),
            'score_medio':round(float(np.mean([r['score'] for r in rows])),2) if rows else 0,
            'ultimo_score':rows[-1]['score'] if rows else 0,'setups':setups}

def executar_backtest(data, estrategias=None):
    if not data or len(data['close']) < 100:
        return {'ok':False,'erro':'Dados insuficientes para backtest V5.'}
    estrategias=estrategias or LISTA_ESTRATEGIAS
    n=len(data['close']); split=max(80,int(n*0.70)); resultados={}
    for est in estrategias:
        rows=_backtest_strategy(data, est, min_score=60, start=60)
        train=[r for r in rows if r['idx'] < split]
        test=[r for r in rows if r['idx'] >= split]
        resultados[est]={
            'nome':NOME_ESTRATEGIAS_DISPLAY.get(est,est),
            **_resumo_backtest(rows),
            'treino':_resumo_backtest(train),
            'fora_amostra':_resumo_backtest(test)
        }
    return {'ok':True,'amostras':n-61,'split_treino':split,'resultados':resultados,'versao':'V5'}

@app.route('/backtest')
def backtest_page():
    user=session.get('user')
    if not user: return redirect('/login')
    st=get_user_state(user)
    return render_template_string('''
<!doctype html><html lang="pt-BR"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Vision Pro V3 Ultra — Laboratório V5</title>
<style>
body{margin:0;background:#070b14;color:#e5e7eb;font-family:Arial,sans-serif;padding:20px}.wrap{max-width:900px;margin:auto}.card{background:#0d1422;border:1px solid #243047;border-radius:16px;padding:18px;margin-bottom:14px}.grid{display:grid;grid-template-columns:1fr 1fr;gap:10px}select,button{width:100%;padding:12px;border-radius:10px;border:1px solid #334155;background:#111827;color:#fff}button{cursor:pointer;font-weight:700;background:#0e7490}table{width:100%;border-collapse:collapse;margin-top:14px}th,td{padding:9px;border-bottom:1px solid #263244;text-align:left;font-size:13px}th{color:#67e8f9}.muted{color:#94a3b8;font-size:12px}.win{color:#34d399}.red{color:#fb7185}.warn{color:#fbbf24}@media(max-width:600px){.grid{grid-template-columns:1fr}}
</style></head><body><div class="wrap"><div class="card"><h2>🧠 Laboratório de Backtest V5</h2><div class="muted">Backtest V5: entrada na abertura da próxima vela e expiração no fechamento dela. O teste é separado em treino e fora da amostra; não é garantia de resultado futuro.</div></div>
<div class="card"><div class="grid"><select id="ativo">{% for a in ativos %}<option value="{{a}}">{{a}}</option>{% endfor %}</select><select id="tf"><option value="1">M1</option><option value="5" {% if tf==5 %}selected{% endif %}>M5</option><option value="15" {% if tf==15 %}selected{% endif %}>M15</option></select></div><button onclick="rodar()" style="margin-top:10px">▶ EXECUTAR BACKTEST</button><button onclick="location.href='/'" style="margin-top:10px;background:#172033">← VOLTAR AO TERMINAL</button></div>
<div id="out" class="card">Escolha o ativo e execute o teste.</div></div>
<script>
async function rodar(){const out=document.getElementById('out');out.innerHTML='⏳ Buscando dados reais e executando análise...';try{const a=document.getElementById('ativo').value,t=document.getElementById('tf').value;const r=await fetch('/api/backtest?ativo='+encodeURIComponent(a)+'&tf='+t);const d=await r.json();if(!d.ok){out.innerHTML='❌ '+d.erro;return}let h='<h3>'+a+' — M'+t+'</h3><div class="muted">'+d.amostras+' pontos históricos avaliados.</div><table><tr><th>Estratégia</th><th>Sinais</th><th>WIN</th><th>RED</th><th>Taxa*</th><th>Fora amostra*</th><th>Score médio</th></tr>';for(const k in d.resultados){const x=d.resultados[k];h+=`<tr><td>${x.nome}</td><td>${x.sinais}</td><td class="win">${x.wins}</td><td class="red">${x.reds}</td><td>${x.taxa}%</td><td>${x.fora_amostra.taxa}% (${x.fora_amostra.sinais})</td><td>${x.score_medio}</td></tr>`}h+='</table>';for(const k in d.resultados){const z=d.resultados[k].setups||{};const ks=Object.keys(z);if(ks.length){h+='<h4>Setups — '+d.resultados[k].nome+'</h4><table><tr><th>Setup</th><th>Sinais</th><th>WIN</th><th>RED</th><th>Taxa*</th></tr>';for(const q of ks){const v=z[q];h+=`<tr><td>${q}</td><td>${v.sinais}</td><td class="win">${v.wins}</td><td class="red">${v.reds}</td><td>${v.taxa}%</td></tr>`}h+='</table>';}}h+='<p class="muted">* Taxa = WIN/(WIN+RED) no teste direcional de 1 candle. Não representa probabilidade nem garante desempenho futuro. V5 separa resultados por setup, janela de entrada e desempenho histórico.</p>';out.innerHTML=h}catch(e){out.innerHTML='❌ Falha ao executar o teste.'}}
</script></body></html>''', ativos=sorted(set(sum(ATIVOS_BASE.values(),[]))), tf=st.get('timeframe',5))

@app.route('/api/backtest')
def api_backtest():
    user=session.get('user')
    if not user: return jsonify({'ok':False,'erro':'Não autenticado.'}),401
    ativo=request.args.get('ativo','').strip().upper()
    try: tf=int(request.args.get('tf',get_user_state(user).get('timeframe',5)))
    except Exception: tf=5
    if ativo not in MAPA_TICKERS or tf not in (1,5,15):
        return jsonify({'ok':False,'erro':'Ativo ou timeframe inválido.'}),400
    data=get_data_v2(MAPA_TICKERS[ativo],tf,velas_minimas=120)
    if not data: return jsonify({'ok':False,'erro':'Não foi possível obter dados reais suficientes para este ativo.'}),503
    return jsonify(executar_backtest(data))

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
        "diagnostico": st.get("diagnostico", {})
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

    if cmd == "test_telegram":
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
        st["inicio_varredura"] = time.time() + 2 
        st["sinais_enviados"].clear()
        st["diagnostico"] = {
            "ciclo_inicio": time.time(), "ciclo_num": 0, "ativos_analisados": 0, "dados_ok": 0, "dados_falha": 0,
            "candidatos": 0, "rejeitados": 0, "oportunidades_validadas": 0, "ultima_oportunidade": None,
            "ultimo_motivo": "Iniciando diagnóstico...", "motivo_contagem": {}, "estrategia_contagem": {},
            "ultimo_score": 0, "ultimo_direcao": None, "ultima_atualizacao": time.time(),
            "estrategias_concordantes": 0, "estrategias_analisadas": 0, "ultimo_ativo_analisado": None,
            "ultimo_setup": None, "ultimo_detalhe": "Preparando varredura...", "ultimo_ciclo_segundos": 0.0, "ativos_por_ciclo": 0
        }
        st["ativo_atual"] = "INICIANDO VARREDURA..."
        st["ultimo_sinal"] = f"<div class='system-console'>⚡ <b>INICIANDO MOTOR DE ANÁLISE DINÂMICA</b><br><span style='color:#00f2fe;'>[VARRENDO TODOS OS ATIVOS...]</span></div><div class='tech-scanner'></div>"
        
        msg_inicio_telegram = (
            f"🚀 <b>SISTEMA VISION PRO V3 INICIADO</b>\n\n"
            f"🟢 <b>Status:</b> Análise de 60 velas ativada\n"
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
        st["ultimo_sinal"] = f"<div class='system-console' style='color:#f59e0b;'>{status_txt}</div>" if st["bot_pausado"] else f"<div class='system-console'>🔍 ANALISANDO 60 VELAS: <b>{st['ativo_atual']}</b> (M{st['timeframe']})<br><span style='color:#00f2fe;'>[VARREDURA CONTINUA]</span></div><div class='tech-scanner'></div>"
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

        alerta_snapshot = dict(alerta)
        ativo = alerta_snapshot["ativo"]
        sinal = alerta_snapshot["sinal"]
        est_fmt = alerta_snapshot["estrategia_fmt"]
        est_key = alerta_snapshot.get("estrategia", est_fmt)
        str_saida = alerta_snapshot["str_saida"]
        prob = alerta_snapshot["probabilidade"]
        tf = alerta_snapshot["tf"]
        str_entrada = alerta_snapshot["str_entrada"]
        analysis_snapshot = dict(alerta_snapshot.get("analise") or {})

        cor_direcao = "#10b981" if sinal == "CALL" else "#ef4444"

        # Atualiza a tela ANTES de qualquer operação de rede/banco.
        st["sinal_permanente"] = (
            f"<div class='status-box' style='border-color:#00f2fe; background:rgba(0,242,254,0.1);'>"
            f"<h3 style='color:#00f2fe; margin-bottom:8px;'>🎯 SINAL CONFIRMADO!</h3>"
            f"<b>ATIVO:</b> {ativo}<br>"
            f"<b>DIREÇÃO DE ENTRADA:</b> <span style='color:{cor_direcao}; font-size:18px;'>{sinal}</span><br>"
            f"<b>ESTRATÉGIA:</b> <span style='color:#38ef7d;'>{est_fmt} | SCORE {prob}/100</span><br>"
            f"<b>TIMEFRAME:</b> M{tf} | <b>ENTRADA:</b> {str_entrada} | <b>EXPIRAÇÃO:</b> {str_saida}"
            f"<br><span style='font-size:11px;color:#94a3b8;'>"
            f"Estrutura: {alerta.get('analise',{}).get('trend','N/D')} | "
            f"Confluências: {alerta.get('analise',{}).get('evidence',0)}"
            f"</span></div>"
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
            f"🔥 <b>Score de Confluência:</b> {prob}/100\n"
            f"🕐 <b>Entrada:</b> {str_entrada}\n"
            f"⌛ <b>Expiração:</b> {str_saida}\n\n"
            f"💡 <i>Gerencie seu capital com responsabilidade.</i>"
        )

        def finalizar_confirmacao(
            _user=user_email, _ativo=ativo, _sinal=sinal,
            _est_fmt=est_fmt, _tf=tf, _msg=msg_sinal
        ):
            try:
                _analysis=analysis_snapshot
                sid=registrar_sinal_bd(_user,f"{_ativo} | {_sinal} | {_est_fmt} | M{_tf}",metadata={
                    "ativo":_ativo,"timeframe":_tf,"estrategia":est_key,"direcao":_sinal,
                    "score":prob,"setup":_analysis.get("setup") or _classificar_setup(_analysis),
                    "analise":_analysis,"entrada_em":agora_brasilia().replace(tzinfo=None)})
                if sid:
                    get_user_state(_user)["ultimo_sinal_id"] = sid
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


# ================= DIAGNÓSTICO V5.1 =================
def _diag_inc(diag, key, amount=1):
    mc=diag.setdefault("motivo_contagem", {})
    mc[key]=mc.get(key,0)+amount

def _diag_strategy_inc(diag, key, amount=1):
    sc=diag.setdefault("estrategia_contagem", {})
    sc[key]=sc.get(key,0)+amount

# ================= LOOP PRINCIPAL MULTI-USUÁRIO DO BOT =================
def bot_loop():
    ohlc_cache = {}
    while True:
        try:
            usuarios_ativos = list(DADOS_USUARIOS.items())
            if not usuarios_ativos:
                time.sleep(1); continue
            now_ts=time.time()
            # Cache curto evita pedir o mesmo ativo repetidamente; a coleta abaixo é paralela.
            ohlc_cache={k:v for k,v in ohlc_cache.items() if now_ts-v["time"]<5}
            for user_email, st in usuarios_ativos:
                try:
                    if not st.get("bot_iniciado") or st.get("bot_pausado"): continue
                    if time.time() < st.get("inicio_varredura",0): continue
                    tf=st.get("timeframe",5); mkt=st.get("tipo_mercado","TODOS"); user_est=st.get("estrategia","TODAS")
                    diag=st.setdefault("diagnostico",{}); cycle_start=time.time()
                    if mkt=="TODOS": ativos=ATIVOS_BASE["FOREX_ABERTO"]+ATIVOS_BASE["CRIPTO_ABERTO"]+ATIVOS_BASE["FOREX_OTC"]+ATIVOS_BASE["CRIPTO_OTC"]
                    elif mkt=="ABERTO_TODOS": ativos=ATIVOS_BASE["FOREX_ABERTO"]+ATIVOS_BASE["CRIPTO_ABERTO"]
                    elif mkt=="OTC_TODOS": ativos=ATIVOS_BASE["FOREX_OTC"]+ATIVOS_BASE["CRIPTO_OTC"]
                    else: ativos=ATIVOS_BASE.get(mkt,ATIVOS_BASE["FOREX_ABERTO"])
                    ativos_scan=ativos.copy(); random.shuffle(ativos_scan)

                    if user_est=="TODAS": estrategias_para_analisar=LISTA_ESTRATEGIAS.copy(); random.shuffle(estrategias_para_analisar)
                    elif "," in str(user_est): estrategias_para_analisar=[e.strip() for e in user_est.split(",") if e.strip() in LISTA_ESTRATEGIAS]
                    elif user_est in LISTA_ESTRATEGIAS: estrategias_para_analisar=[user_est]
                    else: estrategias_para_analisar=LISTA_ESTRATEGIAS.copy()
                    diag["estrategias_analisadas"]=len(estrategias_para_analisar)

                    # COLETA PARALELA: evita que 40–60 ativos levem vários minutos só em timeouts HTTP.
                    futuros={}
                    with ThreadPoolExecutor(max_workers=min(12,max(1,len(ativos_scan)))) as ex:
                        for ativo in ativos_scan:
                            ticker=MAPA_TICKERS.get(ativo,ativo); key=f"{ticker}_{tf}"
                            if key in ohlc_cache: continue
                            futuros[ex.submit(get_data_v2,ticker,tf,60)]=(ativo,key)
                        for fut in as_completed(futuros):
                            ativo,key=futuros[fut]
                            try:
                                d=fut.result()
                                if d: ohlc_cache[key]={"data":d,"time":time.time()}
                            except Exception: pass

                    alerta=st.get("alerta_ativo")
                    bloquear_novos_alertas=st.get("aguardando_confirmacao",False)
                    for ativo in ativos_scan:
                        if not st.get("bot_iniciado") or st.get("bot_pausado"): break
                        st["ativo_atual"]=ativo; diag["ativos_analisados"]=diag.get("ativos_analisados",0)+1; diag["ultimo_ativo_analisado"]=ativo; diag["ultima_atualizacao"]=time.time()
                        ticker=MAPA_TICKERS.get(ativo,ativo); key=f"{ticker}_{tf}"
                        data=ohlc_cache.get(key,{}).get("data")
                        if not alerta and not st.get("aguardando_confirmacao"):
                            st["ultimo_sinal"]=f"<div class='system-console'>🔍 VARRENDO 60 VELAS EM: <b style='color:#00f2fe;font-size:16px;'>{ativo}</b> (M{tf})<br><span style='color:#00f2fe;'>[BUSCANDO CONFLUÊNCIA]</span></div><div class='tech-scanner'></div>"
                        if not data:
                            diag["dados_falha"]=diag.get("dados_falha",0)+1; diag["ultimo_motivo"]=f"{ativo}: sem dados reais suficientes"; diag["ultimo_detalhe"]="Fonte de dados não retornou 60 velas"; _diag_inc(diag,"SEM_DADOS"); continue
                        diag["dados_ok"]=diag.get("dados_ok",0)+1
                        candidatos=[]
                        for est_nome in estrategias_para_analisar:
                            try: sig,p=analisar_estrategia(data,est_nome)
                            except Exception: sig,p=None,0
                            if sig:
                                candidatos.append((est_nome,sig,int(p))); _diag_strategy_inc(diag,est_nome)
                        if not candidatos:
                            diag["ultimo_motivo"]=f"{ativo}: nenhuma estratégia encontrou oportunidade"; diag["ultimo_detalhe"]="Todas as estratégias ficaram sem candidato"; _diag_inc(diag,"SEM_CANDIDATO"); continue
                        diag["candidatos"]=diag.get("candidatos",0)+1; diag["ultima_oportunidade"]=time.time(); diag["ultimo_score"]=max(x[2] for x in candidatos)
                        por_dir={"CALL":[],"PUT":[]}
                        for item in candidatos: por_dir[item[1]].append(item)
                        best_dir=max(por_dir,key=lambda d:(len(por_dir[d]),max([x[2] for x in por_dir[d]],default=0))); best_list=por_dir[best_dir]; best_item=max(best_list,key=lambda x:x[2])
                        sinal_encontrado=best_dir; est_nome_encontrada=best_item[0]; concordancias=len(best_list); maior_prob=best_item[2]+min(10,max(0,concordancias-1)*5)
                        adv_sig,adv_score,adv_details=_advanced_confluence(data)
                        if adv_sig!=sinal_encontrado or adv_score < (62 if concordancias<2 else 55):
                            diag["rejeitados"]=diag.get("rejeitados",0)+1; diag["ultimo_score"]=int(adv_score)
                            if adv_sig!=sinal_encontrado: chave="DIRECAO_CONTRARIA"; motivo=f"{ativo}: candidato {sinal_encontrado}, motor avançado {adv_sig or 'NEUTRO'}"; detalhe=f"{concordancias} estratégia(s) | score avançado {adv_score}/100"
                            else: chave="SCORE_BAIXO"; motivo=f"{ativo}: score avançado {adv_score}/100 abaixo do mínimo {(62 if concordancias<2 else 55)}"; detalhe=f"{concordancias} estratégia(s) concordaram | candidato {best_item[0]}"
                            diag["ultimo_motivo"]=motivo; diag["ultimo_detalhe"]=detalhe; _diag_inc(diag,chave); continue
                        maior_prob=int(round((maior_prob+adv_score)/2.0)); maior_prob=min(100,maior_prob+min(6,max(0,concordancias-1)*3)); maior_prob,adv_details=_enriquecer_score_adaptativo(ativo,tf,est_nome_encontrada,sinal_encontrado,maior_prob,adv_details); adv_details["estrategias_concordantes"]=concordancias; adv_details["estrategias_analisadas"]=len(estrategias_para_analisar)
                        diag["ultimo_score"]=int(maior_prob); diag["estrategias_concordantes"]=concordancias; diag["ultimo_direcao"]=sinal_encontrado; diag["ultimo_setup"]=adv_details.get("setup"); diag["ultimo_detalhe"]=f"{concordancias} concordância(s) | estratégia principal: {est_nome_encontrada} | avançado {adv_score}/100"; diag["ultimo_motivo"]=f"{ativo}: oportunidade VALIDADA — {sinal_encontrado} | score {maior_prob}/100 | {concordancias} concordância(s)"; diag["oportunidades_validadas"]=diag.get("oportunidades_validadas",0)+1
                        if sinal_encontrado and bloquear_novos_alertas:
                            diag["ultimo_motivo"]=f"{ativo}: sinal válido encontrado, mas existe uma entrada aguardando confirmação"; _diag_inc(diag,"AGUARDANDO_CONFIRM"); continue
                        if not sinal_encontrado or bloquear_novos_alertas: continue
                        agora=agora_brasilia(); min_pass=agora.minute%tf; seg_pass=min_pass*60+agora.second; total_seg=tf*60; seg_restantes=total_seg-seg_pass
                        if seg_restantes<=5:
                            diag["rejeitados"]=diag.get("rejeitados",0)+1; diag["ultimo_motivo"]=f"{ativo}: oportunidade encontrada, mas faltavam {int(seg_restantes)}s para a virada — janela perdida"; diag["ultimo_detalhe"]=f"Score {maior_prob}/100 | {concordancias} concordância(s)"; _diag_inc(diag,"JANELA_PERDIDA"); continue
                        prox_minuto_entrada=agora+timedelta(seconds=seg_restantes); momento_confirmacao=prox_minuto_entrada-timedelta(seconds=5); horario_saida=prox_minuto_entrada+timedelta(minutes=tf); str_entrada=momento_confirmacao.strftime("%H:%M:%S"); str_saida=horario_saida.strftime("%H:%M"); nome_est_formatado=NOME_ESTRATEGIAS_DISPLAY.get(est_nome_encontrada,est_nome_encontrada)
                        if alerta:
                            if maior_prob>alerta.get("probabilidade",0):
                                msg_antigo_id=alerta.get("msg_id"); novo_alert_id=str(time.time_ns()); msg_pre_alerta=f"⚡ <b>ALERTA ATUALIZADO: MAIOR CONFLUÊNCIA DETECTADA!</b> ⚡\n\n<b>Ativo:</b> {ativo} (Score {maior_prob}/100)\n<b>Timeframe:</b> M{tf}\n<b>DIREÇÃO DE ENTRADA:</b> {sinal_encontrado}\n<b>Estratégia:</b> {nome_est_formatado}\n<b>Horário da Entrada:</b> {str_entrada}\n\n👉 <i>Alerta anterior cancelado. Abra o ativo {ativo} na corretora!</i>"
                                st["alerta_ativo"]={"ativo":ativo,"sinal":sinal_encontrado,"estrategia":est_nome_encontrada,"estrategia_fmt":nome_est_formatado,"probabilidade":maior_prob,"msg_id":None,"str_entrada":str_entrada,"str_saida":str_saida,"prox_minuto_entrada":prox_minuto_entrada,"momento_confirmacao":momento_confirmacao,"alert_id":novo_alert_id,"tf":tf,"analise":adv_details}
                                if st.get("timer_confirmacao"):
                                    try: st["timer_confirmacao"].cancel()
                                    except Exception: pass
                                atraso=max(0.0,(momento_confirmacao-agora_brasilia()).total_seconds()); t=threading.Timer(atraso,confirmar_alerta_agendado,args=(user_email,novo_alert_id)); t.daemon=True; st["timer_confirmacao"]=t; t.start(); enviar_telegram_em_background(msg_pre_alerta,user_email,alert_id=novo_alert_id,deletar_msg_id=msg_antigo_id,st=st); alerta=st["alerta_ativo"]
                                st["ultimo_sinal"]=f"<div style='text-align:center;color:#f59e0b;'>⚡ <b>ALERTA SUBSTITUÍDO — {maior_prob}/100</b> ⚡<br><b>{ativo}</b> | <b>{sinal_encontrado}</b> | Entrada <b>{str_entrada}</b><br><span style='font-size:12px;color:#00f2fe;'>Estratégia: <b>{nome_est_formatado}</b></span></div>"
                        else:
                            if st["sinais_enviados"].get(ativo)==str_entrada: continue
                            st["sinais_enviados"][ativo]=str_entrada; novo_alert_id=str(time.time_ns()); msg_pre_alerta=f"⚠️ <b>ATENÇÃO: ANALISANDO OPORTUNIDADE DE OPERAÇÃO</b> ⚠️\n\n<b>Ativo:</b> {ativo}\n<b>Timeframe:</b> M{tf}\n<b>DIREÇÃO DE ENTRADA:</b> {sinal_encontrado}\n<b>Estratégia Identificada:</b> {nome_est_formatado}\n<b>Score de Confluência:</b> {maior_prob}/100\n<b>Horário da Entrada:</b> {str_entrada}\n\n👉 <i>Abra o ativo na corretora e prepare-se!</i>"; st["alerta_ativo"]={"ativo":ativo,"sinal":sinal_encontrado,"estrategia":est_nome_encontrada,"estrategia_fmt":nome_est_formatado,"probabilidade":maior_prob,"msg_id":None,"str_entrada":str_entrada,"str_saida":str_saida,"prox_minuto_entrada":prox_minuto_entrada,"momento_confirmacao":momento_confirmacao,"alert_id":novo_alert_id,"tf":tf,"analise":adv_details}; atraso=max(0.0,(momento_confirmacao-agora_brasilia()).total_seconds()); t=threading.Timer(atraso,confirmar_alerta_agendado,args=(user_email,novo_alert_id)); t.daemon=True; st["timer_confirmacao"]=t; t.start(); enviar_telegram_em_background(msg_pre_alerta,user_email,alert_id=novo_alert_id,st=st); st["ultimo_sinal"]=f"<div style='text-align:center;color:#f59e0b;'>⚠️ <b>PREPARE O ATIVO: {ativo} ({maior_prob}/100)</b> ⚠️<br><span style='color:#fff;'>DIREÇÃO: <b>{sinal_encontrado}</b> | Entrada <b>{str_entrada}</b> (M{tf})</span><br><span style='font-size:12px;color:#00f2fe;'>Estratégia: <b>{nome_est_formatado}</b></span></div>"; st["notificacao"]={"id":str(time.time()),"titulo":f"⚠️ PREPARE-SE: {ativo}","corpo":f"Direção: {sinal_encontrado} | Entrada às {str_entrada} (M{tf}) via {nome_est_formatado} ({maior_prob}/100)."}; alerta=st["alerta_ativo"]
                    elapsed=time.time()-cycle_start; diag["ciclo_num"]=diag.get("ciclo_num",0)+1; diag["ultimo_ciclo_segundos"]=round(elapsed,2); diag["ativos_por_ciclo"]=len(ativos_scan); diag["ultima_atualizacao"]=time.time()
                except Exception as e_usr:
                    print(f"Erro no loop do usuario {user_email}: {e_usr}")
            time.sleep(0.5)
        except Exception as err:
            print(f"Erro no loop global do bot: {err}"); time.sleep(2)

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
