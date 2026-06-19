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
    # Local fallback
    cred = credentials.Certificate(os.path.join(BASE_DIR, 'serviceAccountKey.json'))

firebase_admin.initialize_app(cred)
db = firestore.client()

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
                    f"Give 2-3 numbered steps to investigate/resolve, then one follow-up action."
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
            # Verify with Firebase Auth REST API
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

            # Get user role from Firestore
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
            # Create user in Firebase Auth
            user = auth.create_user(email=email, password=password, display_name=name)

            # Create or update user document in Firestore
            db.collection('users').document(user.uid).set({
                'name': name,
                'email': email,
                'role': 'user',
                'created_at': datetime.now().strftime("%Y-%m-%d %H:%M")
            }, merge=True)

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

# ── Routes: Dashboard ─────────────────────────────────────────────────────────
@app.route('/dashboard')
@login_required
def dashboard():
    tickets_ref = db.collection('tickets').order_by('created_at', direction=firestore.Query.DESCENDING)
    all_tickets = [{'id': d.id, **d.to_dict()} for d in tickets_ref.stream()]

    stats = {
        'total':       len(all_tickets),
        'open':        sum(1 for t in all_tickets if t.get('status') == 'Open'),
        'in_progress': sum(1 for t in all_tickets if t.get('status') == 'In Progress'),
        'resolved':    sum(1 for t in all_tickets if t.get('status') == 'Resolved'),
        'critical':    sum(1 for t in all_tickets if t.get('priority') == 'Critical'),
    }
    recent = all_tickets[:5]

    # Chart data
    from collections import Counter
    by_status   = [{'status': k, 'cnt': v} for k, v in Counter(t.get('status','') for t in all_tickets).items()]
    by_priority = [{'priority': k, 'cnt': v} for k, v in Counter(t.get('priority','') for t in all_tickets).items()]
    by_category = [{'category': k, 'cnt': v} for k, v in Counter(t.get('category','') for t in all_tickets).items()]

    return render_template('dashboard.html',
        recent_tickets=recent, stats=stats,
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
    if request.method == 'POST':
        title       = request.form.get('title', '').strip()
        description = request.form.get('description', '').strip()
        priority    = request.form.get('priority', 'Medium')
        category    = request.form.get('category', 'General')
        email       = request.form.get('email', '').strip()

        if not title or not description:
            flash('Title and description are required.', 'error')
            return render_template('ticket_form.html')

        ai_suggestion = get_ai_suggestion(title, description, priority, category)
        now = datetime.now().strftime("%Y-%m-%d %H:%M")

        doc_ref = db.collection('tickets').add({
            'title': title,
            'description': description,
            'status': 'Open',
            'priority': priority,
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
                f"[SupportDesk] Ticket Created",
                f"Hi,\n\nYour ticket '{title}' has been received.\n"
                f"Priority: {priority} | Category: {category}\n\n"
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
    priority    = request.form.get('priority')
    assigned_to = request.form.get('assigned_to', '')
    now = datetime.now().strftime("%Y-%m-%d %H:%M")

    doc_ref = db.collection('tickets').document(ticket_id)
    old = doc_ref.get().to_dict()
    doc_ref.update({'status': status, 'priority': priority, 'assigned_to': assigned_to, 'updated_at': now})

    if old.get('email') and status != old.get('status'):
        simulate_email(old['email'],
            f"[SupportDesk] Ticket Status Updated",
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
    # Delete subcollection comments first
    comments = db.collection('tickets').document(ticket_id).collection('comments').stream()
    for c in comments:
        c.reference.delete()
    db.collection('tickets').document(ticket_id).delete()
    flash('Ticket deleted.', 'success')
    return redirect(url_for('tickets'))

# ── Routes: Admin ─────────────────────────────────────────────────────────────
@app.route('/admin')
@admin_required
def admin():
    tickets_ref = db.collection('tickets').order_by('created_at', direction=firestore.Query.DESCENDING)
    all_tickets = [{'id': d.id, **d.to_dict()} for d in tickets_ref.stream()]

    users_ref = db.collection('users').stream()
    all_users = [{'id': u.id, **u.to_dict()} for u in users_ref]

    email_log = ''
    log_path = os.path.join(BASE_DIR, 'email_log.txt')
    if os.path.exists(log_path):
        with open(log_path) as f:
            email_log = f.read()[-3000:]

    return render_template('admin.html',
        tickets=all_tickets, users=all_users, email_log=email_log)

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

if __name__ == '__main__':
    port = int(os.getenv('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)