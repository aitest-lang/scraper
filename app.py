import os
import time
import random
import logging
import re
from datetime import datetime
from flask import Flask, render_template, request, send_file, jsonify
from flask_sqlalchemy import SQLAlchemy
from bs4 import BeautifulSoup
import requests
from fake_useragent import UserAgent
import csv
import threading

# Initialize Flask App
app = Flask(__name__)
app.config['SQLALCHEMY_DATABASE_URI'] = os.getenv('DATABASE_URL', 'sqlite:///startups.db')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db = SQLAlchemy(app)

# Logging Setup
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
ua = UserAgent()

# Models
class Startup(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(255))
    domain = db.Column(db.String(255), unique=True)
    linkedin = db.Column(db.String(255))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

class Contact(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    startup_id = db.Column(db.Integer, db.ForeignKey('startup.id'))
    email = db.Column(db.String(255))
    source = db.Column(db.String(255))

class Technology(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    startup_id = db.Column(db.Integer, db.ForeignKey('startup.id'))
    tech_name = db.Column(db.String(255))

# Scraping Functions
def get_headers():
    return {'User-Agent': ua.random}

# Google Maps Scraper (via Requests + Parsing)
def scrape_google_maps(location):
    url = f'https://www.google.com/search?q=startups+in+{location}&tbm=lcl'
    try:
        res = requests.get(url, headers=get_headers())
        soup = BeautifulSoup(res.text, 'html.parser')

        for item in soup.select('.tF2Cxc'):
            title = item.select_one('h3').text.strip()
            # Try to extract website link
            try:
                link = item.select_one('a')['href']
                domain = link.split('/')[2]
                logger.info(f"Found: {title} - {domain}")
                startup = Startup(name=title[:255], domain=domain, linkedin=None)
                db.session.add(startup)
                db.session.commit()
            except Exception as e:
                logger.warning(f"Parsing failed: {str(e)}")
    except Exception as e:
        logger.error(f"Google Maps scrape failed: {str(e)}")

# GitHub Scraper
def scrape_github(location):
    url = f"https://api.github.com/search/users?q=location:{location}+job:founder"
    try:
        res = requests.get(url, headers=get_headers())
        users = res.json().get('items', [])
        for user in users:
            profile = requests.get(user['url'], headers=get_headers()).json()
            blog = profile.get('blog', '')
            if blog.startswith(('http://', 'https://')):
                domain = blog.split('/')[2]
                startup = Startup(
                    name=user['login'],
                    domain=domain,
                    linkedin=profile.get('linkedin_username')
                )
                db.session.add(startup)
                db.session.commit()
                logger.info(f"Added GitHub startup: {domain}")
    except Exception as e:
        logger.error(f"GitHub scrape error: {str(e)}")

# Reddit HTML-Based Scraper
def scrape_reddit(location):
    headers = {'User-Agent': ua.random}
    url = f'https://www.reddit.com/r/{location.lower()}startups/new/'
    try:
        res = requests.get(url, headers=headers)
        if res.status_code != 200:
            logger.warning("Reddit rate limit or error")
            return
        soup = BeautifulSoup(res.text, 'html.parser')
        posts = soup.select('a[data-click-id="body"]')
        seen_domains = set()
        
        for post in posts:
            full_url = f"https://reddit.com{post['href']}"
            try:
                domain = full_url.split("/")[2]
                if domain.endswith(".reddit.com") or domain in seen_domains:
                    continue
                seen_domains.add(domain)
                startup = Startup(name="Unknown", domain=domain, linkedin=None)
                db.session.add(startup)
                db.session.commit()
                logger.info(f"Reddit: Added {domain}")
            except IndexError:
                continue
    except Exception as e:
        logger.error(f"Reddit scrape error: {str(e)}")

# Email Discovery
def discover_emails(domain):
    paths = ['', '/about', '/contact', '/careers']
    emails = set()
    for path in paths:
        try:
            res = requests.get(f'https://{domain}{path}', timeout=10, headers=get_headers())
            found = re.findall(r'[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+', res.text)
            emails.update(found)
        except Exception as e:
            logger.debug(f"Email search failed for {path}: {e}")
    for email in emails:
        contact = Contact(email=email, startup_id=1, source='email_discovery')
        db.session.add(contact)
    db.session.commit()

# Tech Detection from Job Page
def detect_tech_stack(domain):
    tech_patterns = {
        'frameworks': ['react', 'vue', 'angular', 'django', 'flask', 'rails'],
        'hosting': ['heroku', 'aws', 'azure', 'firebase', 'netlify']
    }
    try:
        res = requests.get(f'https://{domain}/jobs', timeout=10, headers=get_headers())
        text = res.text.lower()
        for category, patterns in tech_patterns.items():
            for pattern in patterns:
                if pattern in text:
                    tech = Technology(tech_name=pattern, startup_id=1)
                    db.session.add(tech)
        db.session.commit()
    except Exception as e:
        logger.debug(f"Tech detection failed for {domain}: {e}")

# Background Scrape Worker
def run_scraper(location):
    global scraping_status
    scraping_status = {"active": True, "progress": 0, "total": 30}
    
    try:
        t1 = threading.Thread(target=scrape_google_maps, args=(location,))
        t2 = threading.Thread(target=scrape_github, args=(location,))
        t3 = threading.Thread(target=scrape_reddit, args=(location,))
        
        t1.start()
        t2.start()
        t3.start()
        
        while any(t.is_alive() for t in [t1, t2, t3]):
            time.sleep(5)
            scraping_status["progress"] += 1
            
    finally:
        scraping_status["active"] = False
        scraping_status["progress"] = 100
        logger.info("Scraping completed")

# Routes
@app.route('/')
def index():
    return render_template('index.html')

@app.route('/start_scrape', methods=['POST'])
def start_scrape():
    location = request.form.get('location')
    if not location:
        return jsonify({"error": "Location required"}), 400
    thread = threading.Thread(target=run_scraper, args=(location,))
    thread.start()
    return jsonify({"status": "started", "location": location})

@app.route('/status')
def status():
    return jsonify(scraping_status)

@app.route('/export')
def export_data():
    si = StringIO()
    cw = csv.writer(si)
    cw.writerow(['Company Name', 'Domain', 'Emails', 'Technologies', 'LinkedIn'])
    for s in Startup.query.all():
        emails = ';'.join([c.email for c in s.contacts])
        techs = ';'.join([t.tech_name for t in s.technologies])
        cw.writerow([s.name, s.domain, emails, techs, s.linkedin])
    output = make_response(si.getvalue())
    output.headers["Content-Disposition"] = "attachment; filename=startup_data.csv"
    output.headers["Content-type"] = "text/csv"
    return output

# Main
if __name__ == '__main__':
    scraping_status = {"active": False, "progress": 0, "total": 100}
    with app.app_context():
        db.create_all()
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)
