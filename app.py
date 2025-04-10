
from flask import Flask, request, jsonify
from flask_sqlalchemy import SQLAlchemy
from flask_mail import Mail, Message
from apscheduler.schedulers.background import BackgroundScheduler
from twilio.rest import Client
from bs4 import BeautifulSoup
import os
import requests
import datetime

app = Flask(__name__)
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///apartment_ai.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['MAIL_SERVER'] = 'smtp.sendgrid.net'
app.config['MAIL_PORT'] = 587
app.config['MAIL_USE_TLS'] = True
app.config['MAIL_USERNAME'] = 'apikey'
app.config['MAIL_PASSWORD'] = os.getenv("SENDGRID_API_KEY")
app.config['MAIL_DEFAULT_SENDER'] = 'your_email@example.com'
mail = Mail(app)

twilio_sid = os.getenv("TWILIO_SID")
twilio_token = os.getenv("TWILIO_AUTH_TOKEN")
twilio_from = os.getenv("TWILIO_FROM_NUMBER")
twilio_client = Client(twilio_sid, twilio_token)

db = SQLAlchemy(app)

class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(120), unique=True, nullable=False)
    phone = db.Column(db.String(20), nullable=True)
    max_price = db.Column(db.Integer, nullable=False)
    location = db.Column(db.String(120), nullable=False)
    min_bedrooms = db.Column(db.Integer, nullable=False)
    search_active = db.Column(db.Boolean, default=True)
    notify_email = db.Column(db.Boolean, default=True)
    notify_sms = db.Column(db.Boolean, default=False)

class Apartment(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(200))
    price = db.Column(db.Integer)
    location = db.Column(db.String(120))
    bedrooms = db.Column(db.Integer)
    link = db.Column(db.String(300))

class MatchLog(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'))
    message = db.Column(db.String(500))
    timestamp = db.Column(db.DateTime, default=datetime.datetime.utcnow)

with app.app_context():
    db.create_all()

def build_craigslist_url(city):
    domain_map = {
        "New York": "newyork", "San Francisco": "sfbay", "Boston": "boston",
        "Los Angeles": "losangeles", "Chicago": "chicago", "Miami": "miami"
    }
    domain = domain_map.get(city, "sfbay")
    return f"https://{domain}.craigslist.org/search/apa?hasPic=1"

def scrape_craigslist(location):
    url = build_craigslist_url(location)
    try:
        response = requests.get(url)
        soup = BeautifulSoup(response.text, 'html.parser')
        listings = soup.find_all('li', class_='result-row')
        for listing in listings[:5]:
            title = listing.find('a', class_='result-title hdrlnk').text
            link = listing.find('a', class_='result-title hdrlnk')['href']
            price_tag = listing.find('span', class_='result-price')
            price = int(price_tag.text.replace('$', '')) if price_tag else 0
            new_apt = Apartment(title=title[:200], price=price, location=location, bedrooms=1, link=link)
            exists = Apartment.query.filter_by(title=new_apt.title, price=new_apt.price).first()
            if not exists:
                db.session.add(new_apt)
        db.session.commit()
    except Exception as e:
        print(f"Failed to scrape {location}: {e}")

def match_user(user):
    matches = Apartment.query.filter(
        Apartment.price <= user.max_price,
        Apartment.location == user.location,
        Apartment.bedrooms >= user.min_bedrooms
    ).all()
    return matches

def notify_user(user, matches):
    if not matches:
        return

    # AI agent-style message
    log_intro = MatchLog(user_id=user.id, message="🧠 Agent AI: I’ve found new apartments that match your preferences!")
    db.session.add(log_intro)

    email_body = "Hello! Here are your new apartment matches:\n"

    for apt in matches:
        line = f"{apt.title} - ${apt.price} - {apt.link}"
        db.session.add(MatchLog(user_id=user.id, message=line))
        email_body += f"\n{line}"

    if user.notify_email:
        msg = Message(subject="New Apartment Matches", recipients=[user.email])
        msg.body = email_body
        mail.send(msg)

    if user.notify_sms and user.phone:
        try:
            sms_body = f"{len(matches)} new listings under ${user.max_price} in {user.location}. Your AI agent found them!"
            twilio_client.messages.create(body=sms_body, from_=twilio_from, to=user.phone)
        except Exception as e:
            print(f"SMS failed: {e}")

    db.session.commit()

def check_new_listings():
    locations = set()
    active_users = User.query.filter_by(search_active=True).all()
    for user in active_users:
        locations.add(user.location)
    for city in locations:
        scrape_craigslist(city)
    for user in active_users:
        matches = match_user(user)
        if matches:
            notify_user(user, matches)

@app.route('/api/register', methods=['POST'])
def register():
    data = request.json
    new_user = User(
        email=data['email'],
        phone=data.get('phone'),
        max_price=int(data['max_price']),
        location=data['location'],
        min_bedrooms=int(data['min_bedrooms']),
        search_active=True,
        notify_email=data.get('notify_email', True),
        notify_sms=data.get('notify_sms', False)
    )
    db.session.add(new_user)
    db.session.commit()
    return jsonify({"message": "Registered!", "user_id": new_user.id})

@app.route('/api/matches/<int:user_id>')
def get_matches(user_id):
    user = User.query.get(user_id)
    if not user:
        return jsonify({"error": "User not found"}), 404
    logs = MatchLog.query.filter_by(user_id=user_id).order_by(MatchLog.timestamp.desc()).limit(30).all()
    return jsonify({
        "search_active": user.search_active,
        "matches": [
            {"timestamp": log.timestamp.strftime('%H:%M:%S'), "message": log.message}
            for log in logs
        ]
    })

@app.route('/api/toggle/<int:user_id>', methods=['POST'])
def toggle_search(user_id):
    user = User.query.get(user_id)
    if not user:
        return jsonify({"error": "User not found"}), 404
    user.search_active = not user.search_active
    db.session.commit()
    return jsonify({"search_active": user.search_active})

scheduler = BackgroundScheduler()
scheduler.add_job(func=check_new_listings, trigger="interval", minutes=1)
scheduler.start()

if __name__ == "__main__":
    app.run(debug=True)
