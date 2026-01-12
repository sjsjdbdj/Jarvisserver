from flask import Flask, redirect, url_for, session, render_template, request, jsonify, send_file
from authlib.integrations.flask_client import OAuth
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from datetime import datetime, timedelta
from werkzeug.middleware.proxy_fix import ProxyFix
import json
import os
import requests
import io
from urllib.parse import urljoin

# =========================
# CONFIGURACIÓN BÁSICA
# =========================
app = Flask(__name__)

# 🔐 Necesario para Render + Google OAuth
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)
app.config['PREFERRED_URL_SCHEME'] = 'https'

app.secret_key = os.environ.get("FLASK_SECRET_KEY", "0122")

# Configuración para desarrollo local (cambiar en producción)
app.config['SESSION_COOKIE_SECURE'] = False    # True en producción con HTTPS
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'

oauth = OAuth(app)

# =========================
# VARIABLES DE ENTORNO PARA OPENROUTER Y ELEVENLABS
# =========================
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY")
ELEVENLABS_API_KEY = os.environ.get("ELEVENLABS_API_KEY")
ELEVENLABS_VOICE_ID = os.environ.get("ELEVENLABS_VOICE_ID", "Nh2zY9kknu6z4pZy6FhD")
WEATHER_API_KEY = os.environ.get("WEATHER_API_KEY", "8328192daa1342fa9d921712251904")

# =========================
# GOOGLE OAUTH (CON CALENDAR Y TASKS)
# =========================
google = oauth.register(
    name="google",
    client_id=os.environ.get("GOOGLE_CLIENT_ID"),
    client_secret=os.environ.get("GOOGLE_CLIENT_SECRET"),
    server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
    client_kwargs={
        "scope": "openid email profile https://www.googleapis.com/auth/calendar https://www.googleapis.com/auth/tasks"
    }
)

# =========================
# RUTAS DE AUTENTICACIÓN
# =========================

@app.route("/")
def home():
    return render_template("index.html", user=session.get("user"))

@app.route("/login")
def login():
    redirect_uri = url_for("callback", _external=True)
    return google.authorize_redirect(redirect_uri)

@app.route("/login/callback")
def callback():
    try:
        # 1. Obtener el token de Google
        token = google.authorize_access_token()

        # 2. Obtener los datos del usuario DE FORMA CORRECTA
        # La URL correcta está dentro de los metadatos del 'userinfo_endpoint'
        userinfo_endpoint = google.server_metadata.get('userinfo_endpoint')

        if userinfo_endpoint:
            # Usar la URL completa de los metadatos
            resp = google.get(userinfo_endpoint)
            user_info = resp.json()
        else:
            # Fallback: extraer datos básicos directamente del ID Token (JWT)
            # Authlib ya validó este token, es seguro usarlo
            from authlib.jose import jwt
            id_token = token.get('id_token')
            if id_token:
                # Decodificar el ID Token sin verificar (ya lo hizo Google)
                claims = jwt.decode(id_token, options={"verify_signature": False})
                user_info = {
                    'sub': claims.get('sub'),
                    'name': claims.get('name'),
                    'email': claims.get('email'),
                    'picture': claims.get('picture')
                }
            else:
                raise Exception("No se pudo obtener información del usuario")

        # 3. Guardar datos en sesión (CÓDIGO EXISTENTE - MANTENLO)
        session["user"] = user_info
        session["google_token"] = {
            "access_token": token["access_token"],
            "refresh_token": token.get("refresh_token"),
            "token_type": token["token_type"],
            "expires_at": token["expires_at"]
        }

        return redirect(url_for("home"))

    except Exception as e:
        return f"Error en autenticación: {str(e)}", 400

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("home"))

# =========================
# APIS PARA OPENROUTER (CHATGPT)
# =========================

@app.route('/api/openrouter/chat', methods=['POST'])
def openrouter_chat():
    """Endpoint para procesar mensajes con OpenRouter"""
    try:
        if not OPENROUTER_API_KEY:
            return jsonify({'error': 'OpenRouter API key no configurada'}), 500

        data = request.json
        messages = data.get('messages', [])
        
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {OPENROUTER_API_KEY}",
            "HTTP-Referer": request.headers.get('Origin', request.host_url),
            "X-Title": "JARVIS Assistant"
        }

        payload = {
            "model": "openai/gpt-3.5-turbo",
            "messages": messages,
            "max_tokens": 500,
            "temperature": 0.7
        }

        response = requests.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers=headers,
            json=payload,
            timeout=30
        )

        if response.status_code == 200:
            result = response.json()
            if 'choices' in result and len(result['choices']) > 0:
                return jsonify({
                    'success': True,
                    'message': result['choices'][0]['message']['content']
                })
            else:
                return jsonify({'error': 'Respuesta inesperada de OpenRouter'}), 500
        else:
            return jsonify({'error': f'Error {response.status_code} de OpenRouter', 'details': response.text}), response.status_code

    except requests.exceptions.Timeout:
        return jsonify({'error': 'Timeout al conectar con OpenRouter'}), 504
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# =========================
# APIS PARA ELEVENLABS (TEXT-TO-SPEECH)
# =========================

@app.route('/api/elevenlabs/speak', methods=['POST'])
def elevenlabs_speak():
    """Endpoint para convertir texto a voz con ElevenLabs"""
    try:
        if not ELEVENLABS_API_KEY:
            return jsonify({'error': 'ElevenLabs API key no configurada'}), 500

        data = request.json
        text = data.get('text', '')
        
        if not text:
            return jsonify({'error': 'Texto vacío'}), 400

        url = f"https://api.elevenlabs.io/v1/text-to-speech/{ELEVENLABS_VOICE_ID}"
        
        headers = {
            'Accept': 'audio/mpeg',
            'Content-Type': 'application/json',
            'xi-api-key': ELEVENLABS_API_KEY
        }

        payload = {
            "text": text,
            "model_id": "eleven_multilingual_v2",
            "voice_settings": {
                "stability": 0.5,
                "similarity_boost": 0.75,
                "style": 0.3,
                "use_speaker_boost": True
            }
        }

        response = requests.post(url, headers=headers, json=payload, timeout=30)
        
        if response.status_code == 200:
            # Devolver el audio como respuesta binaria
            audio_data = io.BytesIO(response.content)
            audio_data.seek(0)
            
            return send_file(
                audio_data,
                mimetype='audio/mpeg',
                as_attachment=False,
                download_name='jarvis_response.mp3'
            )
        else:
            return jsonify({'error': f'Error {response.status_code} de ElevenLabs', 'details': response.text}), response.status_code

    except requests.exceptions.Timeout:
        return jsonify({'error': 'Timeout al conectar con ElevenLabs'}), 504
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# =========================
# APIS PARA CALENDARIO Y TAREAS (EXISTENTES - MANTENIDAS)
# =========================

def get_google_service(service_name, version):
    """Obtiene un servicio de Google API autenticado"""
    if "google_token" not in session:
        return None

    token = session["google_token"]
    creds = Credentials(
        token=token["access_token"],
        refresh_token=token.get("refresh_token"),
        token_uri="https://oauth2.googleapis.com/token",
        client_id=google.client_id,
        client_secret=google.client_secret,
        scopes=google.client_kwargs["scope"].split()
    )

    return build(service_name, version, credentials=creds)

@app.route('/api/create-event', methods=['POST'])
def create_event():
    if 'google_token' not in session:
        return jsonify({'error': 'No autenticado'}), 401

    try:
        data = request.json
        service = get_google_service('calendar', 'v3')

        # Formatear fechas
        start_time = datetime.fromisoformat(data['startTime'].replace('Z', '+00:00'))
        end_time = datetime.fromisoformat(data['endTime'].replace('Z', '+00:00'))

        event = {
            'summary': data['title'],
            'description': data.get('description', ''),
            'start': {
                'dateTime': start_time.isoformat(),
                'timeZone': 'America/Mexico_City'
            },
            'end': {
                'dateTime': end_time.isoformat(),
                'timeZone': 'America/Mexico_City'
            }
        }

        created_event = service.events().insert(
            calendarId='primary',
            body=event
        ).execute()

        return jsonify({
            'success': True,
            'eventId': created_event['id'],
            'htmlLink': created_event.get('htmlLink'),
            'message': f"Evento '{data['title']}' creado exitosamente"
        })

    except HttpError as error:
        return jsonify({'error': str(error)}), 500
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/create-task', methods=['POST'])
def create_task():
    if 'google_token' not in session:
        return jsonify({'error': 'No autenticado'}), 401

    try:
        data = request.json
        service = get_google_service('tasks', 'v1')

        # Obtener la lista de tareas predeterminada
        tasklists = service.tasklists().list().execute()
        default_tasklist = None

        for tasklist in tasklists.get('items', []):
            if tasklist['title'] == 'Mis tareas' or tasklist['id'] == '@default':
                default_tasklist = tasklist['id']
                break

        if not default_tasklist and tasklists.get('items'):
            default_tasklist = tasklists['items'][0]['id']
        else:
            default_tasklist = '@default'

        task = {
            'title': data['title'],
            'notes': data.get('description', ''),
            'due': data.get('dueDate')
        }

        created_task = service.tasks().insert(
            tasklist=default_tasklist,
            body=task
        ).execute()

        return jsonify({
            'success': True,
            'taskId': created_task['id'],
            'message': f"Tarea '{data['title']}' creada exitosamente"
        })

    except HttpError as error:
        return jsonify({'error': str(error)}), 500
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/list-events', methods=['GET'])
def list_events():
    if 'google_token' not in session:
        return jsonify({'error': 'No autenticado'}), 401

    try:
        service = get_google_service('calendar', 'v3')

        # Obtener eventos de los próximos 7 días
        now = datetime.utcnow().isoformat() + 'Z'
        week_later = (datetime.utcnow() + timedelta(days=7)).isoformat() + 'Z'

        events_result = service.events().list(
            calendarId='primary',
            timeMin=now,
            timeMax=week_later,
            maxResults=10,
            singleEvents=True,
            orderBy='startTime'
        ).execute()

        events = events_result.get('items', [])

        return jsonify({
            'success': True,
            'events': [
                {
                    'id': event['id'],
                    'summary': event.get('summary', 'Sin título'),
                    'start': event['start'].get('dateTime', event['start'].get('date')),
                    'end': event['end'].get('dateTime', event['end'].get('date'))
                }
                for event in events
            ]
        })

    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/list-tasks', methods=['GET'])
def list_tasks():
    if 'google_token' not in session:
        return jsonify({'error': 'No autenticado'}), 401

    try:
        service = get_google_service('tasks', 'v1')

        # Obtener lista predeterminada
        tasklists = service.tasklists().list().execute()
        default_tasklist = '@default'

        if tasklists.get('items'):
            for tasklist in tasklists['items']:
                if tasklist['title'] == 'Mis tareas':
                    default_tasklist = tasklist['id']
                    break

        tasks_result = service.tasks().list(
            tasklist=default_tasklist,
            showCompleted=False,
            showHidden=False
        ).execute()

        tasks = tasks_result.get('items', [])

        return jsonify({
            'success': True,
            'tasks': [
                {
                    'id': task['id'],
                    'title': task['title'],
                    'due': task.get('due'),
                    'completed': task.get('status') == 'completed'
                }
                for task in tasks
            ]
        })

    except Exception as e:
        return jsonify({'error': str(e)}), 500

# =========================
# ENDPOINT PARA PROCESAR COMANDOS DE JARVIS
# =========================

@app.route('/api/process-command', methods=['POST'])
def process_command():
    """Endpoint para procesar comandos naturales de JARVIS"""
    if 'google_token' not in session:
        return jsonify({'error': 'No autenticado'}), 401

    try:
        data = request.json
        command = data.get('command', '').lower()

        # Comandos de calendario
        if any(word in command for word in ['crear evento', 'agendar', 'reunión', 'cita']):
            return jsonify({
                'action': 'create_event',
                'message': 'Para crear un evento, necesito: título, fecha/hora de inicio y fecha/hora de fin.',
                'example': {
                    'title': 'Reunión de equipo',
                    'startTime': '2026-01-15T10:00:00Z',
                    'endTime': '2026-01-15T11:00:00Z',
                    'description': 'Discutir el proyecto JARVIS'
                }
            })

        # Comandos de tareas
        elif any(word in command for word in ['crear tarea', 'añadir tarea', 'recordatorio']):
            return jsonify({
                'action': 'create_task',
                'message': 'Para crear una tarea, necesito el título y opcionalmente una fecha de vencimiento.',
                'example': {
                    'title': 'Revisar documentación',
                    'dueDate': '2026-01-20T00:00:00Z'
                }
            })

        # Listar eventos
        elif any(word in command for word in ['ver eventos', 'listar eventos', 'qué tengo agendado']):
            return jsonify({
                'action': 'list_events',
                'message': 'Obteniendo tus próximos eventos...'
            })

        # Listar tareas
        elif any(word in command for word in ['ver tareas', 'listar tareas', 'qué pendientes tengo']):
            return jsonify({
                'action': 'list_tasks',
                'message': 'Obteniendo tus tareas pendientes...'
            })

        else:
            return jsonify({
                'action': 'unknown',
                'message': 'No reconozco ese comando. Puedo ayudarte con: crear eventos, crear tareas, ver eventos o ver tareas.'
            })

    except Exception as e:
        return jsonify({'error': str(e)}), 500

# =========================
# RUTA PARA OBTENER INFORMACIÓN DE CONFIGURACIÓN
# =========================

@app.route('/api/config')
def get_config():
    """Devuelve configuración pública necesaria para el frontend"""
    return jsonify({
        'openrouter_configured': bool(OPENROUTER_API_KEY),
        'elevenlabs_configured': bool(ELEVENLABS_API_KEY),
        'weather_api_key': WEATHER_API_KEY,
        'base_url': request.host_url
    })

# =========================
# RUTA PARA WEATHER API (PROXY)
# =========================

@app.route('/api/weather', methods=['GET'])
def get_weather():
    """Proxy para WeatherAPI (para evitar problemas de CORS)"""
    try:
        lat = request.args.get('lat')
        lon = request.args.get('lon')
        
        if not lat or not lon:
            return jsonify({'error': 'Se requieren latitud y longitud'}), 400
        
        response = requests.get(
            f'https://api.weatherapi.com/v1/current.json?key={WEATHER_API_KEY}&q={lat},{lon}&lang=es',
            timeout=10
        )
        
        if response.status_code == 200:
            return jsonify(response.json())
        else:
            return jsonify({'error': f'Error obteniendo datos del clima: {response.status_code}'}), response.status_code
            
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# =========================
# MAIN
# =========================
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)