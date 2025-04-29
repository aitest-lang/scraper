# File: app.py
import os
import re
import time
import random
import logging
import requests
from datetime import datetime
from flask import Flask, render_template, request, send_file, jsonify
from flask_sqlalchemy import SQLAlchemy
from stem import Signal
from stem.control import Controller
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from bs4 import BeautifulSoup
import praw
import csv
import threading

# Initialize Flask App
app = Flask(__name__)
app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get('DATABASE_URL', 'sqlite:///startups.db')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db = SQLAlchemy(app)

# Configure Logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

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

# Tor IP Rotation
def renew_tor_ip():
    try:
        with Controller.from_port(port=9051) as controller:
            controller.authenticate()
            controller.signal(Signal.NEWNYM)
        logger.info("Changed Tor IP")
    except Exception as e:
        logger.error(f"IP rotation failed: {str(e)}")

# Configure Selenium with Tor
def create_browser():
    chrome_options = Options()
    chrome_options.add_argument('--proxy-server=socks5://127.0.0.1:9050')
    chrome_options.add_argument('--headless')
    chrome_options.add_argument('--disable-gpu')
    chrome_options.add_argument('--no-sandbox')
    chrome_options.add_argument('--disable-dev-shm-usage')
    chrome_options.add_argument('blink-settings=imagesEnabled=false')
    
    return webdriver.Chrome(options=chrome_options)

# Email Pattern Generator
def generate_email_patterns(name, domain):
    username_patterns = [
        name.split()[0].lower(),  # first name
        name.split()[-1].lower(),  # last name
        name[0].lower() + name.split()[-1].lower(),  # fname initial + lname
        ''.join(name.split()).lower(),  # full name
    ]
    
    for username in username_patterns:
        yield f"{username}@{domain}"
        yield f"{username}_info@{domain}"
        yield f"{username}.info@{domain}"

# Google Maps Scraper
def scrape_google_maps(location):
    browser = create_browser()
    try:
        browser.get(f"https://www.google.com/maps/search/startups+in+{location}")
        scroll_pause = 5
        last_height = browser.execute_script("return document.body.scrollHeight")
        
        while True:
            browser.execute_script("window.scrollTo(0, document.body.scrollHeight);")
            time.sleep(scroll_pause)
            new_height = browser.execute_script("return document.body.scrollHeight")
            
            if new_height == last_height:
                break
            last_height = new_height
            
        soup = BeautifulSoup(browser.page_source, 'html.parser')
        companies = soup.find_all('div', class_='section-result-text-content')
        
        for company in companies:
            name = company.find('h3').text.strip()
            domain = None  # Extract domain from details if available
            if name and domain:
                startup = Startup(name=name, domain=domain)
                db.session.add(startup)
                db.session.commit()
                logger.info(f"Added {name}")
                
    except Exception as e:
        logger.error(f"Google Maps scrape error: {str(e)}")
    finally:
        browser.quit()

# GitHub Scraper
def scrape_github(location):
    headers = {'User-Agent': 'StartupScraper/1.0'}
    try:
        url = f"https://api.github.com/search/users?q=location:{location}+job:founder"
        response = requests.get(url, headers=headers)
        data = response.json()
        
        for item in data.get('items', []):
            github_profile = requests.get(item['url']).json()
            blog_url = github_profile.get('blog', '')
            if blog_url.startswith(('http://', 'https://')):
                domain = blog_url.split('/')[2]
                startup = Startup(
                    name=item['login'],
                    domain=domain,
                    linkedin=github_profile.get('linkedin_username')
                )
                db.session.add(startup)
                db.session.commit()
                logger.info(f"Added GitHub startup: {domain}")
                
    except Exception as e:
        logger.error(f"GitHub scrape error: {str(e)}")

# Reddit Scraper
def scrape_reddit(location):
    try:
        reddit = praw.Reddit(
            user_agent='StartupScraper/1.0',
            timeout=10
        )
        subreddit = reddit.subreddit(f'{location}startups')
        
        for submission in subreddit.new(limit=100):
            domain = submission.url.split('/')[2]
            startup = Startup(
                name=submission.title[:255],
                domain=domain,
                linkedin=None
            )
            db.session.add(startup)
            db.session.commit()
            logger.info(f"Added Reddit startup: {domain}")
            
    except Exception as e:
        logger.error(f"Reddit scrape error: {str(e)}")

# Email Discovery
def discover_emails(domain):
    common_paths = ['/contact', '/about', '/careers', '/contact-us']
    visited = set()
    
    try:
        # Check root page
        response = requests.get(f"https://{domain}", timeout=10)
        emails = re.findall(r"[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+", response.text)
        
        for email in emails:
            contact = Contact(email=email, startup_id=1, source="root")
            db.session.add(contact)
            
        # Check common contact pages
        for path in common_paths:
            if f"https://{domain}{path}" not in visited:
                res = requests.get(f"https://{domain}{path}", timeout=10)
                visited.add(res.url)
                emails += re.findall(r"[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+", res.text)
                
        # Save discovered emails
        for email in set(emails):
            contact = Contact(email=email, startup_id=1, source=path)
            db.session.add(contact)
            
        db.session.commit()
        
    except Exception as e:
        logger.error(f"Email discovery failed for {domain}: {str(e)}")

# Technology Detection
def detect_technology(domain):
    tech_patterns = {
        'frameworks': ['react', 'vue', 'angular', 'django', 'flask', 'rails'],
        'hosting': ['heroku', 'aws', 'azure', 'firebase', 'netlify'],
        'ecommerce': ['shopify', 'woocommerce', 'bigcommerce']
    }
    
    try:
        response = requests.get(f"https://{domain}/jobs", timeout=10)
        text = response.text.lower()
        
        detected = []
        for category, patterns in tech_patterns.items():
            for pattern in patterns:
                if pattern in text:
                    detected.append((category, pattern))
                    
        for category, tech in detected:
            tech_record = Technology(tech_name=tech, startup_id=1)
            db.session.add(tech_record)
            
        db.session.commit()
        
    except Exception as e:
        logger.error(f"Tech detection failed: {str(e)}")

# Background Scraper Thread
def run_scraper(location):
    global scraping_status
    scraping_status = {"active": True, "progress": 0, "total": 30}
    
    try:
        thread1 = threading.Thread(target=scrape_google_maps, args=(location,))
        thread2 = threading.Thread(target=scrape_github, args=(location,))
        thread3 = threading.Thread(target=scrape_reddit, args=(location,))
        
        thread1.start()
        thread2.start()
        thread3.start()
        
        while any(t.is_alive() for t in [thread1, thread2, thread3]):
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
    location = request.form['location']
    if not location:
        return jsonify({"error": "Location is required"}), 400
        
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
    
    startups = Startup.query.all()
    for startup in startups:
        emails = ';'.join([c.email for c in startup.contacts])
        techs = ';'.join([t.tech_name for t in startup.technologies])
        cw.writerow([
            startup.name,
            startup.domain,
            emails,
            techs,
            startup.linkedin
        ])
        
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
