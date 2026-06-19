from flask import Flask, render_template, request, redirect, url_for, flash, jsonify, session
from functools import wraps
import os
import json
import urllib.request
import urllib.error
from datetime import datetime
from dotenv import load_dotenv
import firebase_admin
from firebase_admin import credentials, firestore, auth

load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv('SECRET_KEY', 'change-this-in-production')

ANTHROPIC_API_KEY = os.getenv('ANTHROPIC_API_KEY', '')

# ── Firebase Init ─────────────────────────────────────────────────────────────
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

cred_json = os.getenv('GOOGLE_APPLICATION_CREDENTIALS_JSON')
if cred_json:
    cred_dict = json.loads(cred_json)
    cred = credentials.Certificate(cred_dict)
else:
    cred = credentials.Certificate(os.path.join(BASE_DIR, 'serviceAccountKey.json'))

firebase_admin.initialize_app(cred)
db = firestore.client()

# ── Refresh role from Firestore on every request ──────────────────────────────
from flask import g

@app.before_request
def refresh_user_role():
    if 'uid' in session:
        try:
            user_doc = db.collection('users').document(session['uid']).get()
            if user_doc.exists:
                session['role'] = user_doc.to_dict().get('role', 'user')
        except Exception:
            pass

# ── Auth Decorators ───────────────────────────────────────────────────────────
def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'uid' not in session:
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated

def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'uid' not in session:
            return redirect(url_for('login'))
        if session.get('role') not in ('admin', 'agent'):
            flash('Access denied.', 'error')
            return redirect(url_for('dashboard'))
        return f(*args, **kwargs)
    return decorated

# ── AI Suggestion ─────────────────────────────────────────────────────────────
def get_ai_suggestion(title, description, priority, category):
    if not ANTHROPIC_API_KEY:
        return "Configure ANTHROPIC_API_KEY in your .env to enable AI suggestions."
    try:
        payload = json.dumps({
            "model": "claude-sonnet-4-6",
            "max_tokens": 400,
            "messages": [{
                "role": "user",
                "content": (
                    f"You are a senior technical support engineer. Provide a concise, actionable suggestion "
                    f"to resolve this support ticket.\n\n"
                    f"Title: {title}\nCategory: {category}\nPriority: {priority}\n"
                    f"Description: {description}\n\n"
                    f"Give a 1-2 sentence summary of the most likely cause and the single best first step to resolve it. Be concise."
                )
            }]
        }).encode()
        req = urllib.request.Request(
            "https://api.anthropic.com/v1/messages",
            data=payload,
            headers={
                "Content-Type": "application/json",
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01"
            },
            method="POST"
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
            return data["content"][0]["text"]
    except Exception as e:
        return f"AI suggestion unavailable: {str(e)}"

# ── Email Simulation ──────────────────────────────────────────────────────────
def simulate_email(to_email, subject, body):
    log_path = os.path.join(BASE_DIR, 'email_log.txt')
    with open(log_path, 'a') as f:
        f.write(f"\n{'='*60}\n")
        f.write(f"TO: {to_email}\nSUBJECT: {subject}\nDATE: {datetime.now()}\n\n{body}\n")

# ── Routes: Auth ──────────────────────────────────────────────────────────────
@app.route('/')
def index():
    return redirect(url_for('dashboard') if 'uid' in session else url_for('login'))

@app.route('/login', methods=['GET', 'POST'])
def login():
    if 'uid' in session:
        return redirect(url_for('dashboard'))
    if request.method == 'POST':
        email = request.form.get('email', '').strip()
        password = request.form.get('password', '').strip()
        try:
            api_key = os.getenv('FIREBASE_WEB_API_KEY', '')
            payload = json.dumps({
                "email": email,
                "password": password,
                "returnSecureToken": True
            }).encode()
            req = urllib.request.Request(
                f"https://identitytoolkit.googleapis.com/v1/accounts:signInWithPassword?key={api_key}",
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST"
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read())
                uid = data['localId']

            user_doc = db.collection('users').document(uid).get()
            if not user_doc.exists:
                flash('Account not found. Please contact an administrator.', 'error')
                return render_template('login.html')

            user_data = user_doc.to_dict()
            session['uid'] = uid
            session['email'] = email
            session['name'] = user_data.get('name', email)
            session['role'] = user_data.get('role', 'user')
            flash(f"Welcome back, {session['name']}!", 'success')
            return redirect(url_for('dashboard'))

        except urllib.error.HTTPError as e:
            err = json.loads(e.read())
            msg = err.get('error', {}).get('message', 'Login failed.')
            if 'EMAIL_NOT_FOUND' in msg or 'INVALID_PASSWORD' in msg or 'INVALID_LOGIN_CREDENTIALS' in msg:
                flash('Invalid email or password.', 'error')
            else:
                flash('Login failed. Please try again.', 'error')
        except Exception as e:
            flash('Login failed. Please try again.', 'error')

    return render_template('login.html')

@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        name     = request.form.get('name', '').strip()
        email    = request.form.get('email', '').strip()
        password = request.form.get('password', '').strip()

        if not name or not email or not password:
            flash('All fields are required.', 'error')
            return render_template('register.html')

        try:
            try:
                existing = auth.get_user_by_email(email)
                firestore_doc = db.collection('users').document(existing.uid).get()
                if not firestore_doc.exists:
                    auth.delete_user(existing.uid)
                else:
                    flash('An account with that email already exists.', 'error')
                    return render_template('register.html')
            except auth.UserNotFoundError:
                pass

            user = auth.create_user(email=email, password=password, display_name=name)
            db.collection('users').document(user.uid).set({
                'name': name,
                'email': email,
                'role': 'user',
                'created_at': datetime.now().strftime("%Y-%m-%d %H:%M")
            })
            flash('Account created successfully! You can now log in.', 'success')
            return redirect(url_for('login'))

        except Exception as e:
            msg = str(e)
            if 'EMAIL_EXISTS' in msg or 'already exists' in msg.lower():
                flash('An account with that email already exists.', 'error')
            elif 'WEAK_PASSWORD' in msg:
                flash('Password must be at least 6 characters.', 'error')
            else:
                flash('Registration failed. Please try again.', 'error')

    return render_template('register.html')

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))

# ── Routes: Dashboard ─────────────────────────────────────────────────────────
@app.route('/dashboard')
@login_required
def dashboard():
    role = session.get('role')
    uid  = session.get('uid')
    name = session.get('name', '')

    tickets_ref = db.collection('tickets').order_by('created_at', direction=firestore.Query.DESCENDING)
    all_tickets = [{'id': d.id, **d.to_dict()} for d in tickets_ref.stream()]

    if role == 'user':
        # User sees only their own tickets
        my_tickets = [t for t in all_tickets if t.get('created_by_uid') == uid]
        active    = [t for t in my_tickets if t.get('status') != 'Resolved']
        history   = [t for t in my_tickets if t.get('status') == 'Resolved']
        return render_template('dashboard.html',
            role='user', active_tickets=active, ticket_history=history)

    elif role == 'agent':
        # Agent sees only their assigned tickets
        my_tickets  = [t for t in all_tickets if t.get('assigned_to', '') == name]
        assigned    = sum(1 for t in my_tickets)
        in_progress = sum(1 for t in my_tickets if t.get('status') == 'In Progress')
        resolved    = sum(1 for t in my_tickets if t.get('status') == 'Resolved')
        recent      = my_tickets[:5]
        return render_template('dashboard.html',
            role='agent', assigned=assigned, in_progress=in_progress,
            resolved=resolved, recent_tickets=recent)

    else:
        # Admin sees everything
        from collections import Counter
        stats = {
            'total':       len(all_tickets),
            'open':        sum(1 for t in all_tickets if t.get('status') == 'Open'),
            'in_progress': sum(1 for t in all_tickets if t.get('status') == 'In Progress'),
            'resolved':    sum(1 for t in all_tickets if t.get('status') == 'Resolved'),
            'critical':    sum(1 for t in all_tickets if t.get('priority') == 'Critical'),
        }
        recent      = all_tickets[:5]
        by_status   = [{'status': k, 'cnt': v} for k, v in Counter(t.get('status','') for t in all_tickets).items()]
        by_priority = [{'priority': k, 'cnt': v} for k, v in Counter(t.get('priority','') for t in all_tickets).items()]
        by_category = [{'category': k, 'cnt': v} for k, v in Counter(t.get('category','') for t in all_tickets).items()]
        return render_template('dashboard.html',
            role='admin', recent_tickets=recent, stats=stats,
            by_status=json.dumps(by_status),
            by_priority=json.dumps(by_priority),
            by_category=json.dumps(by_category))

# ── Routes: Tickets ───────────────────────────────────────────────────────────
@app.route('/tickets')
@login_required
def tickets():
    search     = request.args.get('search', '').strip().lower()
    status_f   = request.args.get('status', '')
    priority_f = request.args.get('priority', '')
    category_f = request.args.get('category', '')

    ref = db.collection('tickets').order_by('created_at', direction=firestore.Query.DESCENDING)
    all_tickets = [{'id': d.id, **d.to_dict()} for d in ref.stream()]

    # Agents only see tickets assigned to them
    if session.get('role') == 'agent':
        agent_name = session.get('name', '')
        all_tickets = [t for t in all_tickets if t.get('assigned_to', '') == agent_name]

    if search:
        all_tickets = [t for t in all_tickets if search in t.get('title','').lower() or search in t.get('description','').lower()]
    if status_f:
        all_tickets = [t for t in all_tickets if t.get('status') == status_f]
    if priority_f:
        all_tickets = [t for t in all_tickets if t.get('priority') == priority_f]
    if category_f:
        all_tickets = [t for t in all_tickets if t.get('category') == category_f]

    return render_template('tickets.html',
        tickets=all_tickets, search=search,
        status_f=status_f, priority_f=priority_f, category_f=category_f)

@app.route('/tickets/new', methods=['GET', 'POST'])
@login_required
def new_ticket():
    # Only admins can open tickets on behalf of users
    if session.get('role') != 'admin':
        return redirect(url_for('chat'))

    if request.method == 'POST':
        title       = request.form.get('title', '').strip()
        description = request.form.get('description', '').strip()
        category    = request.form.get('category', 'General')
        email       = request.form.get('email', '').strip()

        if not title or not description:
            flash('Title and description are required.', 'error')
            return render_template('ticket_form.html')

        # Priority defaults to Medium — only admin can change it after creation
        ai_suggestion = get_ai_suggestion(title, description, 'Medium', category)
        now = datetime.now().strftime("%Y-%m-%d %H:%M")

        doc_ref = db.collection('tickets').add({
            'title': title,
            'description': description,
            'status': 'Open',
            'priority': 'Medium',
            'category': category,
            'email': email,
            'assigned_to': '',
            'created_by': session.get('name'),
            'created_by_uid': session.get('uid'),
            'created_at': now,
            'updated_at': now,
            'ai_suggestion': ai_suggestion,
            'email_sent': False
        })
        ticket_id = doc_ref[1].id

        if email:
            simulate_email(email,
                "[SupportDesk] Ticket Created",
                f"Hi,\n\nYour ticket '{title}' has been received.\n"
                f"Category: {category}\n\n"
                f"AI Suggestion:\n{ai_suggestion}\n\nWe'll be in touch soon.\n\n— SupportDesk")

        flash('Ticket created successfully!', 'success')
        return redirect(url_for('ticket_detail', ticket_id=ticket_id))

    return render_template('ticket_form.html')

@app.route('/tickets/<ticket_id>')
@login_required
def ticket_detail(ticket_id):
    doc = db.collection('tickets').document(ticket_id).get()
    if not doc.exists:
        flash('Ticket not found.', 'error')
        return redirect(url_for('tickets'))
    ticket = {'id': doc.id, **doc.to_dict()}
    comments_ref = db.collection('tickets').document(ticket_id).collection('comments').order_by('created_at')
    comments = [{'id': c.id, **c.to_dict()} for c in comments_ref.stream()]
    return render_template('ticket_detail.html', ticket=ticket, comments=comments)

@app.route('/tickets/<ticket_id>/update', methods=['POST'])
@admin_required
def update_ticket(ticket_id):
    status      = request.form.get('status')
    assigned_to = request.form.get('assigned_to', '')
    now = datetime.now().strftime("%Y-%m-%d %H:%M")

    doc_ref = db.collection('tickets').document(ticket_id)
    old = doc_ref.get().to_dict()

    update_data = {
        'status': status,
        'assigned_to': assigned_to,
        'updated_at': now
    }

    # Only admin can change priority
    if session.get('role') == 'admin':
        update_data['priority'] = request.form.get('priority', old.get('priority', 'Medium'))

    doc_ref.update(update_data)

    if old.get('email') and status != old.get('status'):
        simulate_email(old['email'],
            "[SupportDesk] Ticket Status Updated",
            f"Hi,\n\nYour ticket '{old['title']}' status changed to: {status}\n"
            f"Assigned to: {assigned_to or 'Unassigned'}\n\n— SupportDesk")
        doc_ref.update({'email_sent': True})

    flash('Ticket updated.', 'success')
    return redirect(url_for('ticket_detail', ticket_id=ticket_id))

@app.route('/tickets/<ticket_id>/comment', methods=['POST'])
@login_required
def add_comment(ticket_id):
    body = request.form.get('body', '').strip()
    if not body:
        flash('Comment cannot be empty.', 'error')
        return redirect(url_for('ticket_detail', ticket_id=ticket_id))
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    db.collection('tickets').document(ticket_id).collection('comments').add({
        'author': session['name'],
        'body': body,
        'created_at': now
    })
    db.collection('tickets').document(ticket_id).update({'updated_at': now})
    flash('Comment added.', 'success')
    return redirect(url_for('ticket_detail', ticket_id=ticket_id))

@app.route('/tickets/<ticket_id>/delete', methods=['POST'])
@admin_required
def delete_ticket(ticket_id):
    comments = db.collection('tickets').document(ticket_id).collection('comments').stream()
    for c in comments:
        c.reference.delete()
    db.collection('tickets').document(ticket_id).delete()
    flash('Ticket deleted.', 'success')
    return redirect(url_for('tickets'))


# ── Routes: Chat ──────────────────────────────────────────────────────────────
@app.route('/chat')
@login_required
def chat():
    return render_template('chat.html')

@app.route('/chat/message', methods=['POST'])
@login_required
def chat_message():
    data = request.get_json()
    messages = data.get('messages', [])

    if not ANTHROPIC_API_KEY:
        return jsonify({'reply': 'AI is not configured. Please contact your administrator.'})

    try:
        payload = json.dumps({
            'model': 'claude-sonnet-4-6',
            'max_tokens': 500,
            'system': (
                'You are a friendly and professional technical support assistant for SupportDesk. '
                'Help users resolve their technical issues with clear, concise steps. '
                'After 2 exchanges, if the issue is not resolved, naturally suggest that they '
                'can speak with a human representative for further assistance. '
                'Keep responses brief and actionable. Never mention you are Claude or made by Anthropic.'
            ),
            'messages': messages
        }).encode()

        req = urllib.request.Request(
            'https://api.anthropic.com/v1/messages',
            data=payload,
            headers={
                'Content-Type': 'application/json',
                'x-api-key': ANTHROPIC_API_KEY,
                'anthropic-version': '2023-06-01'
            },
            method='POST'
        )
        with urllib.request.urlopen(req, timeout=20) as resp:
            result = json.loads(resp.read())
            return jsonify({'reply': result['content'][0]['text']})
    except Exception as e:
        return jsonify({'reply': 'I am having trouble connecting right now. Please try again.'})

@app.route('/chat/create-ticket', methods=['POST'])
@login_required
def chat_create_ticket():
    data = request.get_json()
    messages = data.get('messages', [])

    # Build summary from chat history
    user_messages = [m['content'] for m in messages if m['role'] == 'user']
    title = user_messages[0][:80] if user_messages else 'Support Request'
    description = chr(10).join([
        f"{'User' if m['role'] == 'user' else 'AI'}: {m['content']}"
        for m in messages
    ])

    # Generate AI suggestion from the full conversation
    ai_suggestion = get_ai_suggestion(title, description, 'Medium', 'General')
    now = datetime.now().strftime('%Y-%m-%d %H:%M')

    doc_ref = db.collection('tickets').add({
        'title': title,
        'description': description,
        'status': 'Open',
        'priority': 'Medium',
        'category': 'General',
        'email': session.get('email', ''),
        'assigned_to': '',
        'created_by': session.get('name'),
        'created_by_uid': session.get('uid'),
        'created_at': now,
        'updated_at': now,
        'ai_suggestion': ai_suggestion,
        'email_sent': False,
        'source': 'chat'
    })
    ticket_id = doc_ref[1].id

    if session.get('email'):
        user_name = session.get('name', '')
        lines = [
            'Hi ' + user_name + ',',
            '',
            'Your support ticket has been created.',
            'Ticket reference: #' + ticket_id,
            'A support agent will be assigned shortly.',
            '',
            '— SupportDesk'
        ]
        simulate_email(
            session['email'],
            '[SupportDesk] Your ticket has been created',
            chr(10).join(lines)
        )

    # Post initial system message to ticket chat
    now2 = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    db.collection('tickets').document(ticket_id).collection('chat_messages').add({
        'author': 'System',
        'role': 'system',
        'body': 'Ticket created. Waiting for a support agent to join...',
        'type': 'event',
        'created_at': now2
    })

    return jsonify({'ticket_id': ticket_id})


# ── Routes: Ticket Chat (real-time polling) ───────────────────────────────────
@app.route('/tickets/<ticket_id>/chat')
@login_required
def ticket_chat(ticket_id):
    doc = db.collection('tickets').document(ticket_id).get()
    if not doc.exists:
        flash('Ticket not found.', 'error')
        return redirect(url_for('tickets'))
    ticket = {'id': doc.id, **doc.to_dict()}
    return render_template('ticket_chat.html', ticket=ticket)

@app.route('/tickets/<ticket_id>/chat/messages')
@login_required
def ticket_chat_messages(ticket_id):
    msgs_ref = db.collection('tickets').document(ticket_id).collection('chat_messages').order_by('created_at')
    messages = [{'id': m.id, **m.to_dict()} for m in msgs_ref.stream()]
    ticket = db.collection('tickets').document(ticket_id).get().to_dict()
    return jsonify({
        'messages': messages,
        'status': ticket.get('status', 'Open'),
        'assigned_to': ticket.get('assigned_to', '')
    })

@app.route('/tickets/<ticket_id>/chat/send', methods=['POST'])
@login_required
def ticket_chat_send(ticket_id):
    data = request.get_json()
    body = data.get('body', '').strip()
    if not body:
        return jsonify({'error': 'Empty message'}), 400
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    db.collection('tickets').document(ticket_id).collection('chat_messages').add({
        'author': session.get('name'),
        'role': session.get('role'),
        'body': body,
        'type': 'message',
        'created_at': now
    })
    db.collection('tickets').document(ticket_id).update({'updated_at': now})
    return jsonify({'ok': True})

@app.route('/tickets/<ticket_id>/chat/join', methods=['POST'])
@admin_required
def ticket_chat_join(ticket_id):
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    name = session.get('name')
    role = session.get('role')
    db.collection('tickets').document(ticket_id).collection('chat_messages').add({
        'author': name,
        'role': role,
        'body': name + ' joined the chat',
        'type': 'event',
        'created_at': now
    })
    if role == 'agent':
        db.collection('tickets').document(ticket_id).update({
            'assigned_to': name,
            'status': 'In Progress',
            'updated_at': now
        })
    return jsonify({'ok': True})

# ── Routes: Admin ─────────────────────────────────────────────────────────────
@app.route('/admin')
@admin_required
def admin():
    user_search = request.args.get('user_search', '').strip().lower()

    tickets_ref = db.collection('tickets').order_by('created_at', direction=firestore.Query.DESCENDING)
    all_tickets = [{'id': d.id, **d.to_dict()} for d in tickets_ref.stream()]

    all_users = [{'id': u.id, **u.to_dict()} for u in db.collection('users').stream()]

    # Filter users by email search
    if user_search:
        all_users = [u for u in all_users if user_search in u.get('email', '').lower()
                     or user_search in u.get('name', '').lower()]

    return render_template('admin.html',
        tickets=all_tickets, users=all_users, user_search=user_search)

@app.route('/admin/users/<uid>/role', methods=['POST'])
@admin_required
def update_user_role(uid):
    if session.get('role') != 'admin':
        flash('Only admins can change roles.', 'error')
        return redirect(url_for('admin'))
    new_role = request.form.get('role')
    if new_role in ('admin', 'agent', 'user'):
        db.collection('users').document(uid).update({'role': new_role})
        flash('User role updated.', 'success')
    return redirect(url_for('admin'))


@app.route('/debug/users')
@admin_required
def debug_users():
    users = []
    for u in db.collection('users').stream():
        data = u.to_dict()
        users.append({'id': u.id, 'name': data.get('name'), 'email': data.get('email'), 'role': repr(data.get('role'))})
    return jsonify(users)

if __name__ == '__main__':
    port = int(os.getenv('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
