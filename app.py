import os
import time
import random
import logging
import re
from datetime import datetime
from io import StringIO
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

# Global State
scraping_status = {
    "active": False,
    "progress": 0,
    "total": 100,
    "location": "",
    "job_title": ""
}

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

# GitHub Scraper (with job title filter)
def scrape_github(location, job_title):
    query = f"location:{location}+job:{job_title}"
    url = f"https://api.github.com/search/users?q={query}"
    try:
        res = requests.get(url, headers=get_headers())
        if res.status_code != 200:
            logger.warning(f"GitHub rate limit or error: {res.status_code}")
            return

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

# Google Maps Scraper
def scrape_google_maps(location, job_title):
    query = f"{job_title} startups in {location}"
    url = f'https://www.google.com/search?q={query}&tbm=lcl'
    try:
        res = requests.get(url, headers=get_headers(), timeout=15)
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

# Reddit HTML-Based Scraper
def scrape_reddit(location, job_title):
    subreddit_map = {
        "remote": "remotework",
        "software": "remotesoftwaredev",
        "ai": "artificial",
        "crypto": "CryptoCurrency"
    }
    subreddit = subreddit_map.get(job_title.lower(), f"{location}startups")

    headers = {'User-Agent': ua.random}
    url = f'https://www.reddit.com/r/{subreddit}/new/'
    try:
        time.sleep(random.uniform(2, 5))  # Rate limiting
        res = requests.get(url, headers=headers, timeout=10)
        if res.status_code != 200:
            logger.warning(f"Reddit returned status {res.status_code}")
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

# Background Scrape Worker
def run_scraper(location, job_title):
    global scraping_status
    scraping_status = {
        "active": True,
        "progress": 0,
        "total": 30,
        "location": location,
        "job_title": job_title
    }
    
    try:
        t1 = threading.Thread(target=scrape_google_maps, args=(location, job_title))
        t2 = threading.Thread(target=scrape_github, args=(location, job_title))
        t3 = threading.Thread(target=scrape_reddit, args=(location, job_title))
        
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
    job_title = request.form.get('job_title')
    if not location or not job_title:
        return jsonify({"error": "Location and job title required"}), 400
    
    thread = threading.Thread(target=run_scraper, args=(location, job_title))
    thread.start()
    
    return jsonify({
        "status": "started",
        "location": location,
        "job_title": job_title
    })

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
    with app.app_context():
        db.create_all()
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)
