from flask import Flask, redirect, url_for, session, render_template, request, jsonify
from authlib.integrations.flask_client import OAuth
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from datetime import datetime, timedelta
from werkzeug.middleware.proxy_fix import ProxyFix
import json
import os

# =========================
# CONFIGURACIÓN BÁSICA
# =========================
app = Flask(__name__)

# 🔐 Necesario para Render + Google OAuth
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)
app.config['PREFERRED_URL_SCHEME'] = 'https'

app.secret_key = "0122"

# Configuración para desarrollo local (cambiar en producción)
app.config['SESSION_COOKIE_SECURE'] = False    # True en producción con HTTPS
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'

oauth = OAuth(app)

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
    if "user" not in session:
        return redirect(url_for("login"))
    return render_template("index.html", user=session["user"])

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
    return redirect(url_for("login"))

# =========================
# APIS PARA CALENDARIO Y TAREAS
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
            # Aquí podrías integrar un parser más sofisticado o llamar a otra IA
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
# MAIN
# =========================
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))

    app.run(host="0.0.0.0", port=port)
