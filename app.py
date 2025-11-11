import os
import secrets
import qrcode

from datetime import datetime
from flask import (
    Flask, render_template, request,
    redirect, url_for, jsonify
)
from flask_sqlalchemy import SQLAlchemy
from flask_mail import Mail, Message
from dotenv import load_dotenv

# ================== LOAD ENV ==================
# Reads values from .env into environment variables in development
load_dotenv()

# ================== FLASK APP CONFIG ==================

app = Flask(__name__)
basedir = os.path.abspath(os.path.dirname(__file__))

app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///' + os.path.join(basedir, 'tickets.db')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

# ---------- EMAIL CONFIG ----------
app.config['MAIL_SERVER'] = os.environ.get('MAIL_SERVER', 'smtp.gmail.com')
app.config['MAIL_PORT'] = int(os.environ.get('MAIL_PORT', 587))
app.config['MAIL_USE_TLS'] = os.environ.get('MAIL_USE_TLS', 'true').lower() == 'true'
app.config['MAIL_USERNAME'] = os.environ.get('MAIL_USERNAME')
app.config['MAIL_PASSWORD'] = os.environ.get('MAIL_PASSWORD')
app.config['MAIL_DEFAULT_SENDER'] = os.environ.get(
    'MAIL_DEFAULT_SENDER',
    f"Event Tickets <{app.config['MAIL_USERNAME']}>" if app.config.get('MAIL_USERNAME') else None
)
mail = Mail(app)

# ---------- MANUAL PAYMENT CONFIG ----------
MANUAL_PAYBILL_NUMBER = os.environ.get("MANUAL_PAYBILL_NUMBER", "522533")
MANUAL_PAY_NAME = os.environ.get("MANUAL_PAY_NAME", "Mtwapa Greenyard Resort")
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "set-a-secure-token")

db = SQLAlchemy(app)

# ensure QR folder exists
os.makedirs(os.path.join(basedir, 'static', 'qrs'), exist_ok=True)

# ================== MODELS ==================

class Event(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(150), nullable=False)
    description = db.Column(db.Text, nullable=False)
    location = db.Column(db.String(150), nullable=False)
    start_time = db.Column(db.DateTime, nullable=False)
    end_time = db.Column(db.DateTime, nullable=False)
    ticket_types = db.relationship('TicketType', backref='event', lazy=True)

class TicketType(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    event_id = db.Column(db.Integer, db.ForeignKey('event.id'), nullable=False)
    name = db.Column(db.String(50), nullable=False)
    price = db.Column(db.Integer, nullable=False)  # in KES
    total_quantity = db.Column(db.Integer, nullable=False)
    sold_quantity = db.Column(db.Integer, default=0)

class Order(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    buyer_name = db.Column(db.String(100), nullable=False)
    buyer_email = db.Column(db.String(120), nullable=False)
    buyer_phone = db.Column(db.String(20), nullable=False)
    payment_method = db.Column(db.String(50), nullable=False)  # 'mpesa_manual'
    payment_status = db.Column(db.String(20), default='pending')  # 'pending','paid','failed'
    mpesa_code = db.Column(db.String(40), nullable=True)
    amount = db.Column(db.Integer, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    # saved so we can create tickets AFTER payment confirmed
    ticket_type_id = db.Column(db.Integer, nullable=True)
    quantity = db.Column(db.Integer, nullable=True)
    stripe_session_id = db.Column(db.String(255), nullable=True)
    mpesa_checkout_request_id = db.Column(db.String(255), nullable=True)

    tickets = db.relationship('Ticket', backref='order', lazy=True)

class Ticket(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    order_id = db.Column(db.Integer, db.ForeignKey('order.id'), nullable=False)
    ticket_type_id = db.Column(db.Integer, db.ForeignKey('ticket_type.id'), nullable=False)
    code = db.Column(db.String(50), unique=True, nullable=False)
    status = db.Column(db.String(20), default='valid')  # 'valid','used'
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    qr_path = db.Column(db.String(200))
    ticket_type = db.relationship('TicketType', backref=db.backref('tickets', lazy=True))

# ================== HELPERS ==================

def generate_ticket_code():
    return secrets.token_hex(4).upper()

def generate_qr(code):
    img = qrcode.make(code)
    rel_path = os.path.join('static', 'qrs', f'{code}.png')
    full_path = os.path.join(basedir, rel_path)
    img.save(full_path)
    return '/' + rel_path

def send_ticket_email(order: Order):
    if not order.buyer_email:
        return
    lines = [
        f"Hi {order.buyer_name},",
        "",
        f"Payment Method: {order.payment_method}",
        f"Payment Status: {order.payment_status}",
        "",
        "Here are your ticket details:",
        "",
    ]
    for t in order.tickets:
        lines.append(
            f"- Event: {t.ticket_type.event.name}, "
            f"Type: {t.ticket_type.name}, "
            f"Code: {t.code}"
        )
    lines.append("")
    lines.append("You can present this email or the QR code at the entrance.")
    body = "\n".join(lines)

    msg = Message(subject="Your Event Ticket(s)", recipients=[order.buyer_email])
    msg.body = body

    for t in order.tickets:
        if t.qr_path:
            fp = os.path.join(basedir, t.qr_path.lstrip('/'))
            if os.path.exists(fp):
                with open(fp, 'rb') as f:
                    msg.attach(
                        filename=os.path.basename(fp),
                        content_type='image/png',
                        data=f.read()
                    )

    mail.send(msg)

def issue_tickets(order: Order, ticket_type: TicketType, quantity: int):
    """Create tickets ONLY when payment is confirmed."""
    if order.tickets:
        return  # already issued
    for _ in range(quantity):
        code = generate_ticket_code()
        qr_path = generate_qr(code)
        t = Ticket(
            order_id=order.id,
            ticket_type_id=ticket_type.id,
            code=code,
            qr_path=qr_path
        )
        db.session.add(t)
    ticket_type.sold_quantity += quantity
    db.session.commit()
    try:
        send_ticket_email(order)
    except Exception as e:
        print("Email error:", e)

# ================== DB SETUP ==================

def setup_db():
    db.create_all()
    if not Event.query.first():
        e = Event(
            name='Pool Party - School Uniform Edition',
            description='Hosted by Britstar Events & Planning',
            location='Greenyard Resort, Mtwapa (Near Cornster Hotel)',
            start_time=datetime(2025, 12, 6, 15, 0),
            end_time=datetime(2025, 12, 7, 5, 0),
        )
        db.session.add(e)
        db.session.commit()

        tt1 = TicketType(event_id=e.id, name='Regular', price=1000, total_quantity=200)
        tt2 = TicketType(event_id=e.id, name='VIP', price=1500, total_quantity=100)
        db.session.add_all([tt1, tt2])
        db.session.commit()

# ================== ROUTES ==================

@app.route('/')
def events():
    events = Event.query.all()
    return render_template('events.html', events=events)

@app.route('/buy/<int:event_id>', methods=['GET', 'POST'])
def buy(event_id):
    event = Event.query.get_or_404(event_id)
    ticket_types = event.ticket_types

    if request.method == 'POST':
        ticket_type_id = int(request.form['ticket_type_id'])
        quantity = int(request.form['quantity'])
        name = request.form['name'].strip()
        email = request.form['email'].strip()
        phone = request.form['phone'].strip()
        payment_method = 'mpesa_manual'
        mpesa_code = request.form.get('mpesa_code', '').strip().upper()

        tt = TicketType.query.get_or_404(ticket_type_id)

        if quantity < 1:
            return "Invalid quantity.", 400
        if tt.sold_quantity + quantity > tt.total_quantity:
            return "Not enough tickets left.", 400

        amount = tt.price * quantity

        order = Order(
            buyer_name=name,
            buyer_email=email,
            buyer_phone=phone,
            payment_method=payment_method,
            payment_status='pending',
            amount=amount,
            ticket_type_id=tt.id,
            quantity=quantity,
            mpesa_code=mpesa_code or None
        )
        db.session.add(order)
        db.session.commit()

        return render_template(
            'mpesa_manual_pending.html',
            order=order,
            event=event,
            paybill_number=MANUAL_PAYBILL_NUMBER,
            pay_name=MANUAL_PAY_NAME
        )

    return render_template(
        'buy.html',
        event=event,
        ticket_types=ticket_types,
        paybill_number=MANUAL_PAYBILL_NUMBER,
        pay_name=MANUAL_PAY_NAME
    )

@app.route('/admin/mark_paid/<int:order_id>')
def admin_mark_paid(order_id):
    token = request.args.get('token')
    if not ADMIN_TOKEN or token != ADMIN_TOKEN:
        return "Forbidden", 403

    order = Order.query.get_or_404(order_id)
    if order.payment_status == 'paid':
        return f"Order {order.id} already marked as paid.", 200

    order.payment_status = 'paid'
    db.session.commit()

    ticket_type = TicketType.query.get(order.ticket_type_id)
    if not ticket_type or not order.quantity:
        return "Ticket type information missing. Cannot issue tickets.", 400

    issue_tickets(order, ticket_type, order.quantity)
    return f"Order {order.id} marked as paid and {order.quantity} ticket(s) issued.", 200

# ---------- Views ----------

@app.route('/order/<int:order_id>')
def order_detail(order_id):
    order = Order.query.get_or_404(order_id)
    return render_template('order_detail.html', order=order)

@app.route('/ticket/<code>')
def ticket_detail(code):
    ticket = Ticket.query.filter_by(code=code).first_or_404()
    return render_template('ticket.html', ticket=ticket)

@app.route('/validate', methods=['GET', 'POST'])
def validate_ticket():
    result = None
    if request.method == 'POST':
        code = request.form['code'].strip().upper()
        ticket = Ticket.query.filter_by(code=code).first()
        if not ticket:
            result = "❌ Invalid ticket."
        elif ticket.status == 'used':
            result = "⚠️ Ticket already used."
        else:
            ticket.status = 'used'
            db.session.commit()
            result = f"✅ Valid ticket: {ticket.ticket_type.event.name} - {ticket.ticket_type.name}"
    return render_template('validate.html', result=result)

# ================== MAIN ==================

if __name__ == '__main__':
    with app.app_context():
        setup_db()
    app.run(
        debug=True,
        host='0.0.0.0',
        port=int(os.environ.get("PORT", 5050))
    )
