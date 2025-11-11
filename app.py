import os
import base64
import secrets
import qrcode
import stripe
import requests

from datetime import datetime
from flask import (
    Flask, render_template, request,
    redirect, url_for, jsonify
)
from flask_sqlalchemy import SQLAlchemy
from flask_mail import Mail, Message

# ================== FLASK APP CONFIG ==================

app = Flask(__name__)
basedir = os.path.abspath(os.path.dirname(__file__))
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///' + os.path.join(basedir, 'tickets.db')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

# ---------- EMAIL CONFIG ----------
app.config['MAIL_SERVER'] = 'smtp.gmail.com'
app.config['MAIL_PORT'] = 587
app.config['MAIL_USE_TLS'] = True
app.config['MAIL_USERNAME'] = 'mtwapagreenyardr@gmail.com'
app.config['MAIL_PASSWORD'] = 'hxdwybadznqorjus'   # Gmail App Password
app.config['MAIL_DEFAULT_SENDER'] = ('Event Tickets', app.config['MAIL_USERNAME'])
mail = Mail(app)

# ---------- M-PESA DARAJA CONFIG ----------
MPESA_BASE_URL = os.environ.get("MPESA_BASE_URL", "https://sandbox.safaricom.co.ke")
MPESA_SHORTCODE = os.environ.get("MPESA_SHORTCODE", "522533")
MPESA_PASSKEY = os.environ.get(
    "MPESA_PASSKEY",
    "bfb279f9aa9bdbcf158e97dd71a467cd2e0c893059b10f78e6b72ada1ed2c919"
)
MPESA_CONSUMER_KEY = os.environ.get(
    "MPESA_CONSUMER_KEY",
    "k9lTB3WN8GxlNjimgsAGhGiO3qVB3emDXf0FVBxS7r13aZvl"
)
MPESA_CONSUMER_SECRET = os.environ.get(
    "MPESA_CONSUMER_SECRET",
    "d2lhUKP7AkeCLVAFPCa7xeG7scn01fdbMaL2IwifE24oBWrLOkCQzWuL8GQxjU3y"
)
MPESA_CALLBACK_URL = os.environ.get(
    "MPESA_CALLBACK_URL",
    "https://example.com/mpesa/callback"
)

# ---------- STRIPE CONFIG (CARD) ----------
STRIPE_SECRET_KEY = os.environ.get(
    "STRIPE_SECRET_KEY",
    "stripe_secret_placeholder"
)
STRIPE_PUBLISHABLE_KEY = os.environ.get(
    "STRIPE_PUBLISHABLE_KEY",
    "stripe_publishable_placeholder"
)
stripe.api_key = STRIPE_SECRET_KEY

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
    payment_method = db.Column(db.String(50), nullable=False)  # 'mpesa' or 'card'
    payment_status = db.Column(db.String(20), default='pending')  # 'pending','paid','failed'
    amount = db.Column(db.Integer, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

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
        return
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

# ---------- M-Pesa helpers ----------

def get_mpesa_access_token():
    resp = requests.get(
        f"{MPESA_BASE_URL}/oauth/v1/generate?grant_type=client_credentials",
        auth=(MPESA_CONSUMER_KEY, MPESA_CONSUMER_SECRET),
        timeout=10
    )
    resp.raise_for_status()
    return resp.json().get('access_token')

def initiate_mpesa_stk(phone_number: str, amount: int, order_id: int):
    """Initiate STK push and return CheckoutRequestID if accepted."""
    access_token = get_mpesa_access_token()
    timestamp = datetime.now().strftime('%Y%m%d%H%M%S')
    password = base64.b64encode(
        (MPESA_SHORTCODE + MPESA_PASSKEY + timestamp).encode()
    ).decode()

    url = f"{MPESA_BASE_URL}/mpesa/stkpush/v1/processrequest"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json"
    }
    payload = {
        "BusinessShortCode": MPESA_SHORTCODE,
        "Password": password,
        "Timestamp": timestamp,
        "TransactionType": "CustomerPayBillOnline",
        "Amount": amount,
        "PartyA": phone_number,
        "PartyB": MPESA_SHORTCODE,
        "PhoneNumber": phone_number,
        "CallBackURL": MPESA_CALLBACK_URL,
        "AccountReference": f"ORDER{order_id}",
        "TransactionDesc": "Event Ticket Payment"
    }

    resp = requests.post(url, json=payload, headers=headers, timeout=10)
    try:
        data = resp.json()
    except Exception:
        data = {}

    print("M-Pesa STK raw response:", resp.status_code, data)

    if resp.status_code == 200 and str(data.get("ResponseCode")) == "0":
        return data.get("CheckoutRequestID")
    return None

def query_mpesa_stk(checkout_request_id: str):
    """Use STK Query API to confirm payment status."""
    access_token = get_mpesa_access_token()
    timestamp = datetime.now().strftime('%Y%m%d%H%M%S')
    password = base64.b64encode(
        (MPESA_SHORTCODE + MPESA_PASSKEY + timestamp).encode()
    ).decode()

    url = f"{MPESA_BASE_URL}/mpesa/stkpushquery/v1/query"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json"
    }
    payload = {
        "BusinessShortCode": MPESA_SHORTCODE,
        "Password": password,
        "Timestamp": timestamp,
        "CheckoutRequestID": checkout_request_id
    }

    resp = requests.post(url, json=payload, headers=headers, timeout=10)
    try:
        data = resp.json()
    except Exception:
        data = {}

    print("M-Pesa STK Query response:", resp.status_code, data)

    if resp.status_code == 200 and str(data.get("ResultCode")) == "0":
        return True
    return False

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
        tt1 = TicketType(event_id=e.id, name='Regular', price=100, total_quantity=70)
        tt2 = TicketType(event_id=e.id, name='VIP', price=1500, total_quantity=250)
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
        payment_method = request.form['payment_method']  # 'mpesa' or 'card'

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
            quantity=quantity
        )
        db.session.add(order)
        db.session.commit()

        # ----- M-Pesa -----
        if payment_method == 'mpesa':
            try:
                checkout_id = initiate_mpesa_stk(phone, amount, order.id)
            except Exception as e:
                print("M-Pesa STK error:", e)
                order.payment_status = 'failed'
                db.session.commit()
                return "Failed to initiate M-Pesa STK.", 400

            if not checkout_id:
                order.payment_status = 'failed'
                db.session.commit()
                return "M-Pesa STK was not accepted. Try again.", 400

            order.mpesa_checkout_request_id = checkout_id
            db.session.commit()
            return render_template('mpesa_pending.html', order=order)

        # ----- Card (Stripe Checkout) -----
        elif payment_method == 'card':
            try:
                # Charge in KES (Stripe uses smallest unit)
                currency = 'kes'
                unit_amount = tt.price * 100  # 500 KES -> 50000

                checkout_session = stripe.checkout.Session.create(
                    payment_method_types=['card'],
                    mode='payment',
                    line_items=[{
                        'price_data': {
                            'currency': currency,
                            'product_data': {
                                'name': f"{event.name} - {tt.name} Ticket"
                            },
                            'unit_amount': unit_amount,
                        },
                        'quantity': quantity,
                    }],
                    success_url=url_for(
                        'card_success', order_id=order.id, _external=True
                    ) + '?session_id={CHECKOUT_SESSION_ID}',
                    cancel_url=url_for(
                        'card_cancel', order_id=order.id, _external=True
                    ),
                )
            except Exception as e:
                print("Stripe error:", e)
                order.payment_status = 'failed'
                db.session.commit()
                return "Failed to start card payment.", 400

            order.stripe_session_id = checkout_session['id']
            db.session.commit()
            return redirect(checkout_session.url, code=303)

        else:
            order.payment_status = 'failed'
            db.session.commit()
            return "Unknown payment method.", 400

    return render_template('buy.html', event=event, ticket_types=ticket_types)

# ---------- Stripe: success / cancel ----------

@app.route('/card/success/<int:order_id>')
def card_success(order_id):
    order = Order.query.get_or_404(order_id)
    session_id = request.args.get('session_id')

    if not session_id or order.stripe_session_id != session_id:
        return "Invalid session.", 400

    try:
        session = stripe.checkout.Session.retrieve(session_id)
    except Exception as e:
        print("Stripe retrieve error:", e)
        return "Could not verify payment.", 400

    if session.payment_status == 'paid':
        if order.payment_status != 'paid':
            order.payment_status = 'paid'
            db.session.commit()
            tt = TicketType.query.get(order.ticket_type_id)
            if tt and order.quantity:
                issue_tickets(order, tt, order.quantity)
    else:
        order.payment_status = 'failed'
        db.session.commit()
        return "Payment not completed.", 400

    return redirect(url_for('order_detail', order_id=order.id))

@app.route('/card/cancel/<int:order_id>')
def card_cancel(order_id):
    order = Order.query.get_or_404(order_id)
    if order.payment_status != 'paid':
        order.payment_status = 'failed'
        db.session.commit()
    return "Card payment cancelled. No ticket generated."

# ---------- M-Pesa: manual check with STK Query ----------

@app.route('/mpesa/check/<int:order_id>')
def mpesa_check(order_id):
    order = Order.query.get_or_404(order_id)

    if not order.mpesa_checkout_request_id:
        return "No M-Pesa request found for this order.", 400

    if order.payment_status == 'paid':
        return redirect(url_for('order_detail', order_id=order.id))

    try:
        success = query_mpesa_stk(order.mpesa_checkout_request_id)
    except Exception as e:
        print("M-Pesa STK Query error:", e)
        return "Could not verify M-Pesa payment. Try again.", 400

    if not success:
        return (
            "M-Pesa payment not confirmed yet. "
            "If money was deducted, wait a bit then click 'Check payment' again. "
            "No ticket has been generated.",
            400
        )

    order.payment_status = 'paid'
    db.session.commit()

    tt = TicketType.query.get(order.ticket_type_id)
    if tt and order.quantity:
        issue_tickets(order, tt, order.quantity)

    return redirect(url_for('order_detail', order_id=order.id))

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
        # If models changed, run once:
        # rm tickets.db
        setup_db()
    app.run(debug=True, host='0.0.0.0', port=int(os.environ.get("PORT", 5050)))
