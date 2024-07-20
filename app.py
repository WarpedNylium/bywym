from flask import Flask, render_template, request, jsonify
import feedparser
from flask_sqlalchemy import SQLAlchemy
from flask_caching import Cache
from bs4 import BeautifulSoup
from datetime import datetime, timedelta
from dateutil.parser import parse
from dateutil.tz import tzutc
from requests.exceptions import Timeout
import pytz
import json
import requests
import html
import os
import aiohttp
import asyncio
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.events import EVENT_JOB_MISSED
from asgiref.wsgi import WsgiToAsgi
import logging
import urllib
from transformers import BartTokenizer, BartForConditionalGeneration
from transformers import T5Tokenizer, T5ForConditionalGeneration
import traceback
from http.client import IncompleteRead
from dateutil.tz import gettz
from dateutil.parser import ParserError

#AI models
english_model = BartForConditionalGeneration.from_pretrained('facebook/bart-large-cnn')
english_tokenizer = BartTokenizer.from_pretrained('facebook/bart-large-cnn')
japanese_tokenizer = T5Tokenizer.from_pretrained('tsmatz/mt5_summarize_japanese')
japanese_model = T5ForConditionalGeneration.from_pretrained('tsmatz/mt5_summarize_japanese')

#Logging to capture all messages of level INFO
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# Filesystem cache
cache_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'cache')
os.makedirs(cache_dir, exist_ok=True)
cache = Cache(config={'CACHE_TYPE': 'filesystem', 'CACHE_DIR': cache_dir})
cache_ready = False

def my_listener(event):
    if event.code == EVENT_JOB_MISSED:
        logging.warning(f"Job was missed: {event.job_id}")

# Urls for News displayed in homepage 
URLS = [
    'https://www.cbsnews.com/latest/rss/main',
    'http://feeds.bbci.co.uk/news/rss.xml',
    'https://www.nhk.or.jp/rss/news/cat0.xml',
    'https://www.aljazeera.com/xml/rss/all.xml',
    'https://www.theguardian.com/world/rss',
    'https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=100003114'
]

#Makes an asynchronous GET request to specified URLs
async def fetch(session, url):
        async with session.get(url, timeout=1000) as response:
            return await response.text()
        
#Define various parsing logic for specified URLs
async def parse_feed(session, url):
            loop = asyncio.get_event_loop()
            feed = await loop.run_in_executor(None, feedparser.parse, url)
            entries = []

            if 'bbci.co.uk' in url:
                feed.entries = feed.entries[:12]  # Change 12 to the number of entries you want
            if 'aljazeera.com' in url:
                feed.entries = feed.entries[:12]  
            if 'nhk.or.jp' in url:
                feed.entries = feed.entries[:12]  
            if 'theguardian.com' in url:
                feed.entries = feed.entries[:12]
            if 'cbsnews.com' in url:
                feed.entries = feed.entries[:12]
            if 'cnbc.com' in url:
                feed.entries = feed.entries[:12]

            #Only process articles published in the last 24 hours
            for entry in feed.entries:
                if hasattr(entry, 'published_parsed'):
                    published_time = datetime(*entry.published_parsed[:6], tzinfo=pytz.UTC)
                    current_time = datetime.now(pytz.UTC)
                    twenty_four_hours_ago = current_time - timedelta(hours=24)
                    if published_time < twenty_four_hours_ago:
                        continue  # Skip this entry if it's older than 24 hours
                try:
                    page_html = await fetch(session, entry.link)
                    soup = BeautifulSoup(page_html, 'html.parser')
                    if 'nhk.or.jp' in url:
                        script_tags = soup.find_all('script', type='application/ld+json')
                        for script_tag in script_tags:
                            data = json.loads(script_tag.string)
                            if data.get('@type') == 'NewsArticle' and 'image' in data and len(data['image']) > 0:
                                entry['image_url'] = data['image'][0]['url']
                                break
                
                    elif 'aljazeera.com' in url:
                        script_tag = soup.find('script', type='application/ld+json')
                        if script_tag is not None:
                            data = json.loads(script_tag.string)
                            if 'image' in data and isinstance(data['image'], list) and 'url' in data['image'][0]:
                                entry['image_url'] = data['image'][0]['url']
                            else:
                                entry['image_url'] = 'https://encrypted-tbn0.gstatic.com/images?q=tbn:ANd9GcQ9sXvoSpXCYsdA1qW74Q3uGp8CAN19bUWGoQ&s'
                        else:
                            entry['image_url'] = 'https://encrypted-tbn0.gstatic.com/images?q=tbn:ANd9GcQ9sXvoSpXCYsdA1qW74Q3uGp8CAN19bUWGoQ&s'

                    elif 'bbci.co.uk' in url:
                        if 'media_thumbnail' in entry and len(entry['media_thumbnail']) > 0:
                            entry['image_url'] = entry['media_thumbnail'][0]['url']
                        else:
                            entry['image_url'] = 'https://upload.wikimedia.org/wikipedia/commons/thumb/e/ea/BBC_World_News_2022_%28Boxed%29.svg/800px-BBC_World_News_2022_%28Boxed%29.svg.png'
                        p_tag = soup.find('p', class_='sc-eb7bd5f6-0 fYAfXe')
                        if p_tag is not None:
                            lines = p_tag.get_text(separator='\n').split('\n')
                            entry['summary'] = '\n'.join(lines[:3]) + '...'

                    elif 'theguardian.com' in url:
                        if 'media_content' in entry and len(entry['media_content']) > 0:
                            entry['image_url'] = entry['media_content'][0]['url']
                        else:
                            entry['image_url'] = 'https://encrypted-tbn0.gstatic.com/images?q=tbn:ANd9GcQR_E1vW-vT3q3rKzhtxt6MHMezjtmOp3_5dg&s'
                        soup = BeautifulSoup(entry['summary'], 'html.parser') 
                        for div in soup.find_all("div", {'class':' dcr-4gwv1z'}):
                            div.decompose() 
                        summary_text = soup.get_text()
                        summary_words = summary_text.split()  # Split the text by spaces to get a list of words
                        max_words = 40  # Set your desired maximum number of words
                        entry['summary'] = ' '.join(summary_words[:max_words]) + ',continued ... '

                    elif 'cbsnews.com' in url:
                        response = requests.get(entry.link)
                        soup = BeautifulSoup(response.text, 'html.parser')
                        image_tag = soup.find('link', {'rel': 'preload', 'as': 'image'})
                        if image_tag:
                            entry['image_url'] = image_tag['href']
                        else:
                            entry['image_url'] = 'https://e7.pngegg.com/pngimages/901/52/png-clipart-cbs-corporation-logo-united-states-of-america-television-betting-television-text.png'
                        if 'summary' in entry:
                            soup = BeautifulSoup(entry['summary'], 'html.parser')
                            summary_text = soup.get_text()
                            summary_lines = summary_text.split('\n')  # Split the text by newline characters
                            entry['summary'] = '\n'.join(summary_lines[:1]) 

                    elif 'cnbc.com' in url:
                        response = requests.get(entry.link)
                        soup = BeautifulSoup(response.text, 'html.parser')
                        script_tag = soup.find('script', type='application/ld+json')
                        if script_tag:
                            data = json.loads(script_tag.string)
                            entry['image_url'] = data.get('image', {}).get('url', 'https://upload.wikimedia.org/wikipedia/commons/4/4c/CNBC_logo.svg')
                        else:
                            entry['image_url'] = 'https://sc.cnbcfm.com/applications/cnbc.com/staticcontent/img/cnbc_logo.gif'
                    if 'summary' in entry:
                        entry['summary'] = html.unescape(entry['summary'])
                    entries.append(entry)
                    
                except Exception as e:
                    print(f"An error occurred: {e}")
                    entry['image_url'] = 'https://encrypted-tbn0.gstatic.com/images?q=tbn:ANd9GcQ9w-00zDGFh6VtxNsOtRMeflVFF6GQunbMrA&s'
                    entries.append(entry) # Add this line here

                publish_date = entry.get('published')
                if publish_date:
                    try:
                        if isinstance(publish_date, str):
                            try:
                                publish_date = parse(publish_date)
                            except ParserError:
                                publish_date = parse(publish_date, tzinfos={"JST": gettz("Asia/Tokyo")})
                        if publish_date.tzinfo is not None:  # if datetime object is offset-aware
                            publish_date = publish_date.astimezone(tzutc()).replace(tzinfo=None)  # convert to offset-naive
                    except ValueError:
                        publish_date = None
                entry['publish_date'] = publish_date    
            entries = sorted(entries, key=lambda e: (e['publish_date'] is None, e['publish_date']), reverse=True)
            return entries #Display the entries in descending order of published date

#Fetches and parses a web feed from a given URL and store it in the cache
async def fetch_and_parse(url):
    print(f"Fetching and parsing {url}")  
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    async with aiohttp.ClientSession() as session:
        for _ in range(3):  # Retry up to 3 times
            try:
                entries = await parse_feed(session, url)
                cache.set(url, entries)
                print(f"Cache set for {url} with {len(entries)} entries")
                break
            except IncompleteRead:
                print(f"IncompleteRead error when fetching {url}, retrying...")
                continue

#Used to set job scheduler to fetch and parse the URLs every 60 minutes
def sync_fetch_and_parse(url):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(fetch_and_parse(url))
    finally:
        loop.close()

#Fetches and parses multiple web feeds concurrently
async def fetch_all(urls):
        async with aiohttp.ClientSession() as session:
            tasks = []
            for url in urls:
                tasks.append(parse_feed(session, url))
            all_entries = await asyncio.gather(*tasks)
        return [entry for entries in all_entries for entry in entries]

#Set Database
db = SQLAlchemy()
class Article(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(100), nullable=False)
    url = db.Column(db.String(500), nullable=False)  # URL of the article
    published_date = db.Column(db.DateTime)  # Date the article was published
    comments = db.relationship('Comment', backref='article', lazy='dynamic') # One-to-many relationship with Comment
    
class Comment(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    content = db.Column(db.String(500), nullable=False)
    article_id = db.Column(db.Integer, db.ForeignKey('article.id'))

#Create the Flask app to enable Scheduler, cache and database
def create_app():
    app = Flask(__name__)
    app.config['CACHE_TYPE'] = 'filesystem'
    cache.init_app(app)

    jobstores = {
        'default': SQLAlchemyJobStore(url='sqlite:///jobs.sqlite')
    }

    scheduler = BackgroundScheduler(jobstores=jobstores)
    scheduler.add_listener(my_listener, EVENT_JOB_MISSED)

    app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///app.db'  # Use your actual database URI
    db.init_app(app)

    # Import your models here
    with app.app_context():
        db.create_all()  # Create the database tables
    
    def initialize():
        logging.info("Initializing.....")
        global cache_ready
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        tasks = [fetch_and_parse(url) for url in URLS]
        loop.run_until_complete(asyncio.gather(*tasks))
        for url in URLS:
            scheduler.add_job('app:sync_fetch_and_parse', 'interval', minutes=60, args=(url,))
            print(f"Added job to fetch and parse {url} every 60 minutes")
        cache_ready = True

    initialize()
    scheduler.start()
    print("Scheduler started...")
    return app

app = create_app()
print("Flask app created successfully...")

#Post comments
@app.route('/comments', methods=['POST'])
def post_comment():
    data = request.get_json()
    content = data.get('content')
    article_id = data.get('article_id')  # Get the article ID from the JSON data
    if content and article_id:
        article = Article.query.get(article_id)  # Query by ID to get the article
        if article is None:
            return 'Article not found', 404
        comment = Comment(content=content, article_id=article.id)
        db.session.add(comment)
        db.session.commit()
        return '', 200
    else:
        return 'Invalid input', 400

#Get comments
@app.route('/articles/<int:article_id>/comments', methods=['GET'])
def get_comments(article_id):
    article = Article.query.get(article_id)
    if article is None:
        return 'Article not found', 404
    comments = [comment.content for comment in article.comments]
    return jsonify(comments)

# summay NHK
def parse_nhk_article(url):
    response = requests.get(url)
    response.encoding = 'utf-8'
    soup = BeautifulSoup(response.text, 'html.parser') 
    # Find the summary and p text
    summary = soup.find('p', {'class': 'content--summary'})
    summary_text = summary.get_text(separator=' ') if summary else ''  
    div_tags = soup.find_all('div', class_='body-text')
    body_text_lines = []
    for div in div_tags:
        p_tag = div.find('p')
        if p_tag is not None:
            body_text_lines.append(p_tag.get_text(separator=' '))
    # Join the body text lines together
    body_text = ' '.join(body_text_lines)  
    # Combine the summary and body text and return the result
    return summary_text + ' ' + body_text
# summay BBC
def parse_bbc_article(url):
    response = requests.get(url)
    soup = BeautifulSoup(response.text, 'html.parser') 
    p_tags = soup.find_all('p', class_='sc-eb7bd5f6-0 fYAfXe')
    lines = [p.get_text() for p in p_tags]
    # Join the lines together and return the result
    return ' '.join(lines)
# summay Aljazeera
def parse_aljazeera_article(url):
    response = requests.get(url)
    soup = BeautifulSoup(response.text, 'html.parser') 
    # Find the <div> with the specified class
    div = soup.find('div', class_='wysiwyg wysiwyg--all-content css-ibbk12')
    if div is None:
        return None
    p_tags = div.find_all('p')
    lines = [p.get_text() for p in p_tags]
    return ' '.join(lines)
# summay CBS
def parse_cbsnews_article(url):
    response = requests.get(url)
    soup = BeautifulSoup(response.text, 'html.parser') 
    # Find the <section> with the specified class
    section = soup.find('section', class_='content__body')
    if section is None:
        return None
    p_tags = section.find_all('p')
    lines = [p.get_text() for p in p_tags]
    return ' '.join(lines)
# summay NTV
def parse_ntv_article(url):
    response = requests.get(url)
    soup = BeautifulSoup(response.text, 'html.parser')
    p_tag = soup.find('p', {'class': 'player-text'})
    if p_tag is None:
        return None
    text = p_tag.get_text()
    return text
# summay Guardian
def parse_guardian_article(url):
    response = requests.get(url)
    soup = BeautifulSoup(response.text, 'html.parser')
    # Find all <p> tags with the specified class
    paragraphs = soup.find_all('p', class_=['dcr-iy9ec7', 'dcr-jdlpgv', 'dcr-shm5ll', 'dcr-ntq2eh'])
    text = [p.get_text() for p in paragraphs]
    return text        
# summay UN
def parse_un_article(url):
    response = requests.get(url)
    soup = BeautifulSoup(response.text, 'html.parser')
    # Find the <div> tag with the specified class
    div = soup.find('div', class_='clearfix text-formatted field field--name-field-text-column field--type-text-long field--label-hidden field__item')
    paragraphs = div.find_all('p') if div else []
    text = [p.get_text() for p in paragraphs]
    return text
# summay UN Sustainablity
def parse_un_sustainablity_article(url):
    response = requests.get(url)
    soup = BeautifulSoup(response.text, 'html.parser')
    # Find all <p> tags with the specified class
    paragraphs = [p for p in soup.find_all('p', class_='story-body__introduction') if not p.find('span')]
    # Extract the text from each paragraph
    text = [p.get_text() for p in paragraphs]
    return text
# summay UNEP
def parse_unep_article(url):
    headers = {
        'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_5) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/83.0.4103.97 Safari/537.36',
    }
    response = requests.get(url, headers=headers)
    soup = BeautifulSoup(response.text, 'html.parser')
    # Find all <div> tags with the specified class
    divs = soup.find_all('div', class_='paragraph paragraph--type--content paragraph--view-mode--default')
    # Find all <p> tags within each <div> tag and extract the text
    text = [p.get_text() for div in divs for p in div.find_all('p')]
    return text
# summay Politico(Error 403)
def parse_politico_article(url):
    headers = {
        'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_5) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/83.0.4103.97 Safari/537.36',
    }
    response = requests.get(url, headers=headers)
    soup = BeautifulSoup(response.text, 'html.parser')
    # Find all <p> tags with the specified class
    paragraphs = soup.find_all('p', class_='story-text__paragraph  ')
    # Extract the text from each paragraph
    text = [p.get_text() for p in paragraphs]
    return text
# summay CNBC
def parse_cnbc_article(url):
    response = requests.get(url)
    soup = BeautifulSoup(response.text, 'html.parser')
    # Find all <div> tags with class 'group'
    divs = soup.find_all('div', class_='group')
    # For each div, find all <p> tags and extract the text
    text = [p.get_text() for div in divs for p in div.find_all('p')]
    return text
# summay WebMD
def parse_webmd_article(url):
    headers = {
        'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_5) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/83.0.4103.97 Safari/537.36',
    }
    response = requests.get(url, headers=headers)
    print(f"Response status: {response.status_code}")
    soup = BeautifulSoup(response.text, 'html.parser')
    # Find the div with the specified classes
    article_body = soup.find('div', class_='article-body')
    article_page = article_body.find('div', class_='article-page active-page') if article_body else None
    # Find all <p> tags within the div
    paragraphs = article_page.find_all('p') if article_page else []
    # Extract the text from each paragraph
    text = [p.get_text() for p in paragraphs]
    return text
# summay ENN
def parse_enn_article(url):
    response = requests.get(url)
    soup = BeautifulSoup(response.text, 'html.parser')
    # Find the section with the specified class and attribute
    article_content = soup.find('section', class_='article-content', itemprop='articleBody')
    # Find all <p> tags within the section
    paragraphs = article_content.find_all('p') if article_content else []
    # Extract the text from each paragraph
    text = [p.get_text() for p in paragraphs]
    return text
# summay Inside Climate News
def parse_insideclimatenews_article(url):
    response = requests.get(url)
    soup = BeautifulSoup(response.text, 'html.parser')
    # Find the div with the specified class
    entry_content = soup.find('div', class_='entry-content')
    # Find all <p> tags within the div
    paragraphs = entry_content.find_all('p') if entry_content else []
    # Extract the text from each paragraph
    text = [p.get_text() for p in paragraphs]
    return text
# summay CNN
def parse_cnn_article(url):
    response = requests.get(url)
    soup = BeautifulSoup(response.text, 'html.parser')
    # Find all <p> tags with the specified class
    paragraphs = soup.find_all('p', class_='paragraph inline-placeholder vossi-paragraph-primary-core-light')
    # Extract the text from each <p> tag
    text = [p.get_text() for p in paragraphs]
    return text
# summay NPR
def parse_npr_article(url):
    response = requests.get(url)
    soup = BeautifulSoup(response.text, 'html.parser')
    # Find all the <p> tags
    paragraphs = soup.find_all('p')
    # Extract the text from each paragraph
    text = [p.get_text() for p in paragraphs]
    return text

#Parsing functions for different websites' summary                     
parsing_functions = {
    'www3.nhk.or.jp': parse_nhk_article,
    'www.bbc.com': parse_bbc_article,
    'www.aljazeera.com': parse_aljazeera_article,
    'www.cbsnews.com': parse_cbsnews_article,
    'news.ntv.co.jp': parse_ntv_article,
    'www.theguardian.com' : parse_guardian_article,
    'news.un.org' : parse_un_article,
    'www.un.org' : parse_un_sustainablity_article,
    'www.unep.org' : parse_unep_article,
    'www.politico.com' : parse_politico_article,
    'www.cnbc.com' : parse_cnbc_article,    
    'www.webmd.com': parse_webmd_article,
    'www.enn.com': parse_enn_article,
    'insideclimatenews.org': parse_insideclimatenews_article,
    'www.cnn.com': parse_cnn_article,
    'www.npr.org': parse_npr_article,
    # Add more as needed
}

#Settings for AI models
def summarize_article(article, url):
    try:
        # Choose the model and tokenizer based on the URL(Japan)
        if 'www3.nhk.or.jp' in url or 'news.ntv.co.jp' in url:
            model = japanese_model
            tokenizer = japanese_tokenizer
        else:
            model = english_model  
            tokenizer = english_tokenizer  

        # Split the article into chunks of max_position_embeddings tokens
        chunk_size = getattr(model.config, 'max_position_embeddings', 512)  # Use 512 as default if attribute does not exist
        chunks = [article[i:i + chunk_size] for i in range(0, len(article), chunk_size)]

        summaries = []
        for chunk in chunks:
            # Ensure chunk is a single string
            if isinstance(chunk, list):
                chunk = ' '.join(chunk)

            # Tokenize the chunk
            inputs = tokenizer([chunk], max_length=1024 , return_tensors='pt', truncation=True)

            # Generate a summarized version of the chunk
            try:
                max_length = min(100, model.config.max_position_embeddings)
            except AttributeError:
                max_length = 100  # Default value if 'max_position_embeddings' does not exist

            summary_ids = model.generate(inputs['input_ids'], num_beams=4, max_length=max_length, min_length=40, early_stopping=True)
            summary = [tokenizer.decode(g, skip_special_tokens=True, clean_up_tokenization_spaces=False) for g in summary_ids]

            summaries.append(summary[0])

        # Join the summaries together and return the result
        return ' '.join(summaries)
    except Exception as e:
        print("Error in summarize_article:", str(e))
        print(traceback.format_exc())
        return None

#Summarize the article by sending it to AI models
@app.route('/summarize', methods=['POST'])
def summarize():
    try:
        url = request.form.get('url')
        # Determine which parsing function to use
        base_url = urllib.parse.urlparse(url).netloc
        parse_article = parsing_functions.get(base_url)
        if parse_article is None:
            return jsonify(error='No article has been found'), 400

        article = parse_article(url)
        # Send the article to the AI for summarization
        summary = summarize_article(article, url)  # Pass the URL to summarize_article
        if summary is None:
            return jsonify(error='Error summarizing article'), 500

        return jsonify(summary=summary)
    except Exception as e:

        return jsonify(error='Server error'), 500

@app.route("/")
def home():
    print("Rendering home page...")
    entries = []
    for url in URLS:
        feed_entries = cache.get(url)
        if feed_entries is not None:
            for entry in feed_entries:
                if entry['publish_date'] is not None:
                    try:
                        entry['publish_date'] = parse(str(entry['publish_date']))
                    except (ValueError, ParserError):
                        entry['publish_date'] = None
            entries.extend(feed_entries)
    far_future = datetime.now() + timedelta(days=100*365) #Give articles without a publish date a far future date
    entries.sort(key=lambda e: e['publish_date'] if e['publish_date'] is not None else far_future, reverse=True)
    print("Home page rendered successfully...")
    return render_template('IN Homepage.html', feed=entries)

#Different Agencies
@app.route("/CNN")
@cache.cached(timeout=900)
def CNN():
    feed = feedparser.parse('http://rss.cnn.com/rss/cnn_latest.rss')
    entries = []
    articles = []  # Initialize the articles list
    for entry in feed.entries:
        response = requests.get(entry['link'])
        soup = BeautifulSoup(response.text, 'html.parser')
        json_script = soup.find('script', type='application/ld+json')
        image_url = None
        if json_script:
            data = json.loads(json_script.string)
            if isinstance(data, dict):
                image_url = data.get('thumbnailUrl')
            elif isinstance(data, list):
                for item in data:
                    if isinstance(item, dict):
                        image_url = item.get('thumbnailUrl')
                        if image_url:
                            break
        if image_url:
            entry['image_url'] = image_url
        else:
            entry['image_url'] = 'https://upload.wikimedia.org/wikipedia/commons/thumb/b/b1/CNN.svg/1200px-CNN.svg.png'
        # Check if an article with the same URL already exists in the database
        article = Article.query.filter_by(url=entry.link).first()
        # If not, create a new Article instance and add it to the database
        if article is None:
            article = Article(
                title=entry.title,
                url=entry.link,
                published_date=datetime(*entry.published_parsed[:6])  # Convert time_struct to datetime
            )
            db.session.add(article)
            db.session.flush()  # Flush the session to assign an ID to the article
        # Add the Article instance to the entry
        entry['article'] = article
        entries.append(entry)
        articles.append(article)
        # Check if the article is in the database
        #article_in_db = Article.query.get(article.id)
        #if article_in_db is not None:
            #print(f"Article is in the database: {article_in_db.id}")  # Debug statement
        #else:
            #print(f"Article is not in the database: {article.id}")  # Debug statemente database
    db.session.commit()

    return render_template('All_agencies/CNN.html', feed=entries, articles=articles)

@app.route("/BBC")
@cache.cached(timeout=900)
def BBC():
    urls = ['http://feeds.bbci.co.uk/news/rss.xml', 'http://feeds.bbci.co.uk/news/world/rss.xml']
    entries = []
    articles = []  # Initialize the articles list
    for url in urls:
        feed = feedparser.parse(url)
        for i, entry in enumerate(feed.entries):
            if i < 10 :  # Only process the first 10 entries
                if 'media_thumbnail' in entry and len(entry['media_thumbnail']) > 0:
                    entry['image_url'] = entry['media_thumbnail'][0]['url']
                else:
                    entry['image_url'] = 'https://upload.wikimedia.org/wikipedia/commons/thumb/e/ea/BBC_World_News_2022_%28Boxed%29.svg/800px-BBC_World_News_2022_%28Boxed%29.svg.png'
                response = requests.get(entry.link)
                soup = BeautifulSoup(response.text, 'html.parser') 
                # Find the <p> tag with class 'player-text' and extract its text
                p_tag = soup.find('p', class_='sc-eb7bd5f6-0 fYAfXe')
                if p_tag is not None:
                    lines = p_tag.get_text(separator='\n').split('\n')
                    entry['summary'] = '\n'.join(lines[:3]) + '...'

                # Check if an article with the same URL already exists in the database
                article = Article.query.filter_by(url=entry.link).first()
                # If not, create a new Article instance and add it to the database
                if article is None:
                    article = Article(
                        title=entry.title,
                        url=entry.link,
                        published_date=datetime(*entry.published_parsed[:6])  # Convert time_struct to datetime
                    )
                    db.session.add(article)
                    db.session.flush()  # Flush the session to assign an ID to the article
                # Add the Article instance to the entry
                entry['article'] = article
                entries.append(entry)
                articles.append(article)
                # Check if the article is in the database
                #article_in_db = Article.query.get(article.id)
                #if article_in_db is not None:
                    #print(f"Article is in the database: {article_in_db.id}")  # Debug statement
                #else:
                    #print(f"Article is not in the database: {article.id}")  # Debug statemente database
    db.session.commit()

    # Sort the entries based on their published time
    entries = sorted(entries, key=lambda e: e.published_parsed, reverse=True)
    return render_template('All_agencies/BBC.html', feed=entries, articles=articles)

@app.route("/Guardian")
@cache.cached(timeout=900)
def Guardian():
    urls = ['https://www.theguardian.com/uk/rss','https://www.theguardian.com/world/rss']
    entries = []
    articles = []  # Initialize the articles list
    for url in urls:
        feed = feedparser.parse(url)
        for entry in feed.entries:
            if 'media_content' in entry and len(entry['media_content']) > 0:
                entry['image_url'] = entry['media_content'][0]['url']
            else:
                entry['image_url'] = 'https://encrypted-tbn0.gstatic.com/images?q=tbn:ANd9GcQR_E1vW-vT3q3rKzhtxt6MHMezjtmOp3_5dg&s'
            soup = BeautifulSoup(entry['summary'], 'html.parser')
            for div in soup.find_all("div", {'class':' dcr-4gwv1z'}):
                div.decompose()
            summary_text = soup.get_text()
            summary_words = summary_text.split()  # Split the text by spaces to get a list of words
            max_words = 75  # Set your desired maximum number of words
            entry['summary'] = ' '.join(summary_words[:max_words]) + ',continued ... '
            article = Article.query.filter_by(url=entry.link).first()
            # If not, create a new Article instance and add it to the database
            if article is None:
                if 'published_parsed' in entry:
                    published_date = datetime(*entry.published_parsed[:6])  # Convert time_struct to datetime
                else:
                    published_date = datetime.now()
                article = Article(
                    title=entry.title,
                    url=entry.link,
                    published_date=published_date  
                )
                db.session.add(article)
                db.session.flush()  # Flush the session to assign an ID to the article
            # Add the Article instance to the entry
            entry['article'] = article
            entries.append(entry)
            articles.append(article)
            # Check if the article is in the database
            #article_in_db = Article.query.get(article.id)
            #if article_in_db is not None:
                #print(f"Article is in the database: {article_in_db.id}")  # Debug statement
            #else:
                #print(f"Article is not in the database: {article.id}")  # Debug statemente database
    db.session.commit()

    # Sort the entries based on their published time
    entries = sorted(entries, key=lambda e: e.published_parsed, reverse=True)
    return render_template('All_agencies/Guardian.html', feed=entries, articles=articles)

@app.route("/NPR")
@cache.cached(timeout=900)
def NPR():
    feed = feedparser.parse('https://www.npr.org/rss/rss.php?id=1001')
    entries = []
    articles = []  # Initialize the articles list
    for entry in feed.entries:
        response = requests.get(entry.link)
        soup = BeautifulSoup(response.text, 'html.parser')
        
        # Find the first <picture> tag
        picture_tag = soup.find('picture')
        if picture_tag:
            # Find the first <img> tag within the <picture> tag
            img_tag = picture_tag.find('img')
            if img_tag and 'src' in img_tag.attrs:
                # If an image URL is found, add it to the entry
                entry['image_url'] = img_tag['src']
            else:
                # If no image URL is found, use a default image URL
                entry['image_url'] = 'https://upload.wikimedia.org/wikipedia/commons/thumb/d/d7/National_Public_Radio_logo.svg/1200px-National_Public_Radio_logo.svg.png'
        article = Article.query.filter_by(url=entry.link).first()
        # If not, create a new Article instance and add it to the database
        if article is None:
            if 'published_parsed' in entry:
                published_date = datetime(*entry.published_parsed[:6])  # Convert time_struct to datetime
            else:
                published_date = datetime.now()
            article = Article(
                title=entry.title,
                url=entry.link,
                published_date=published_date  
            )
            db.session.add(article)
            db.session.flush()  # Flush the session to assign an ID to the article
        # Add the Article instance to the entry
        entry['article'] = article
        entries.append(entry)
        articles.append(article)
        # Check if the article is in the database
        #article_in_db = Article.query.get(article.id)
        #if article_in_db is not None:
            #print(f"Article is in the database: {article_in_db.id}")  # Debug statement
        #else:
            #print(f"Article is not in the database: {article.id}")  # Debug statemente database
    db.session.commit()
    return render_template('All_agencies/NPR.html', feed=entries, articles=articles)

@app.route("/CBS")
@cache.cached(timeout=900)
def CBS():
    feed = feedparser.parse('https://www.cbsnews.com/latest/rss/main')
    entries = []
    articles = []  # Initialize the articles list
    for entry in feed.entries:
        response = requests.get(entry.link)
        soup = BeautifulSoup(response.text, 'html.parser')
        image_tag = soup.find('link', {'rel': 'preload', 'as': 'image'})
        if image_tag:
            entry['image_url'] = image_tag['href']
        else:
            entry['image_url'] = 'https://e7.pngegg.com/pngimages/901/52/png-clipart-cbs-corporation-logo-united-states-of-america-television-betting-television-text.png'
        # Check if an article with the same URL already exists in the database
        article = Article.query.filter_by(url=entry.link).first()
        if article is None:
            if 'published_parsed' in entry:
                published_date = datetime(*entry.published_parsed[:6])  # Convert time_struct to datetime
            else:
                published_date = datetime.now()
            article = Article(
                title=entry.title,
                url=entry.link,
                published_date=published_date  
            )
            db.session.add(article)
            db.session.flush()  # Flush the session to assign an ID to the article
        # Add the Article instance to the entry
        entry['article'] = article
        entries.append(entry)
        articles.append(article)
        # Check if the article is in the database
        #article_in_db = Article.query.get(article.id)
        #if article_in_db is not None:
            #print(f"Article is in the database: {article_in_db.id}")  # Debug statement
        #else:
            #print(f"Article is not in the database: {article.id}")  # Debug statement
    db.session.commit()
    return render_template('All_agencies/CBS.html', feed=entries, articles=articles)

@app.route("/NewYorkTimes")
@cache.cached(timeout=900)
def NewYorkTimes():
    urls = ['https://rss.nytimes.com/services/xml/rss/nyt/HomePage.xml', 'https://rss.nytimes.com/services/xml/rss/nyt/World.xml','https://rss.nytimes.com/services/xml/rss/nyt/AsiaPacific.xml']
    entries = []
    articles = []  # Initialize the articles list
    for url in urls:
        feed = feedparser.parse(url)
        for entry in feed.entries:
            if 'media_content' in entry and len(entry['media_content']) > 0:
                entry['image_url'] = entry['media_content'][0]['url']
            else:
                entry['image_url'] = 'https://ropercenter.cornell.edu/sites/default/files/styles/800x600/public/Images/New-York-Times-Logo8x6_0.png?itok=7YqGOSMA'
        # Check if an article with the same URL already exists in the database
        article = Article.query.filter_by(url=entry.link).first()
        if article is None:
            if 'published_parsed' in entry:
                published_date = datetime(*entry.published_parsed[:6])  # Convert time_struct to datetime
            else:
                published_date = datetime.now()
            article = Article(
                title=entry.title,
                url=entry.link,
                published_date=published_date  
            )
            db.session.add(article)
            db.session.flush()  # Flush the session to assign an ID to the article
        # Add the Article instance to the entry
        entry['article'] = article
        entries.append(entry)
        articles.append(article)
        # Check if the article is in the database
        #article_in_db = Article.query.get(article.id)
        #if article_in_db is not None:
            #print(f"Article is in the database: {article_in_db.id}")  # Debug statement
        #else:
            #print(f"Article is not in the database: {article.id}")  # Debug statement
    db.session.commit()
    # Sort the entries based on their published time
    entries = sorted(entries, key=lambda e: e.published_parsed, reverse=True)
    return render_template('All_agencies/NewYorkTimes.html', feed=entries,articles=articles)
    
@app.route("/NHK") 
@cache.cached(timeout=900)
def NHK():   
    urls = ['https://www.nhk.or.jp/rss/news/cat0.xml', 'https://www.nhk.or.jp/rss/news/cat-live.xml', 'https://www.nhk.or.jp/rss/news/cat4.xml']  
    articles = []
    entries = []
    for url in urls:
        feed = feedparser.parse(url)
        for entry in feed.entries:
            published_time = datetime(*entry.published_parsed[:6])
            published_time = pytz.UTC.localize(published_time)

            # Get the current time
            current_time = datetime.now(pytz.UTC)
            # Only process the entry if it was published in the last 90 hours
            if current_time - published_time > timedelta(hours=90):
                continue  # Skip this entry
            # Only process the entry if it was published in the last 24 hours
            if current_time - published_time <= timedelta(hours=90):
                response = requests.get(entry.link)
                soup = BeautifulSoup(response.text, 'html.parser')
                entry['image_url'] = 'https://upload.wikimedia.org/wikipedia/commons/4/4c/NHK_logo_2020.svg'
                script_tags = soup.find_all('script', type='application/ld+json')  # Find all script tags with type 'application/ld+json'
                for script_tag in script_tags:
                    data = json.loads(script_tag.string)  # Parse the JSON data
                    if data.get('@type') == 'NewsArticle' and 'image' in data and len(data['image']) > 0:
                        entry['image_url'] = data['image'][0]['url']  # Get the image URL
                        break  # Exit the loop once the image URL is found
            article = Article.query.filter_by(url=entry.link).first()
            # If not, create a new Article instance and add it to the database
            if article is None:
                if 'published_parsed' in entry:
                    published_date = datetime(*entry.published_parsed[:6])  # Convert time_struct to datetime
                else:
                    published_date = datetime.now()
                article = Article(
                    title=entry.title,
                    url=entry.link,
                    published_date=published_date  
                )
                db.session.add(article)
                db.session.flush()  # Flush the session to assign an ID to the article
            # Add the Article instance to the entry
            entry['article'] = article
            entries.append(entry)
            articles.append(article)
            # Check if the article is in the database
            #article_in_db = Article.query.get(article.id)
            #if article_in_db is not None:
                #print(f"Article is in the database: {article_in_db.id}")  # Debug statement
            #else:
                #print(f"Article is not in the database: {article.id}")  # Debug statemente database
    db.session.commit()
    # Sort the entries based on their published time
    entries = sorted(entries, key=lambda e: e.published_parsed, reverse=True)
    return render_template('All_agencies/NHK.html', feed=entries,articles=articles)

@app.route("/日テレNEWS_NNN")
@cache.cached(timeout=900)
def 日テレNEWS_NNN():
    feed = feedparser.parse('https://news.ntv.co.jp/rss/index.rdf')
    articles = []  # Initialize the articles list
    entries = []  # Initialize the entries list
    for i, entry in enumerate(feed.entries):
        if i >= 20:  
            break
        response = requests.get(entry.link)
        soup = BeautifulSoup(response.text, 'html.parser')
        image_tag = soup.find('img')
        if image_tag is not None:
            entry['image_url'] = image_tag['src']
        else:
            entry['image_url'] = 'https://www.ntv.co.jp/assets/images/meta/og-image.png'
         # Find the <p> tag with class 'player-text' and extract its text
        p_tag = soup.find('p', class_='player-text')
        if p_tag is not None:
            lines = p_tag.get_text(separator='\n').split('\n')
            entry['summary'] = '\n'.join(lines[:2]) + '...'   
        # Check if an article with the same URL already exists in the database
        article = Article.query.filter_by(url=entry.link).first()
         # If not, create a new Article instance and add it to the database
        if article is None:
            try:
                published_date = parse(entry.dc_date)  # Parse the 'dc:date' element into a datetime object
            except (AttributeError, ValueError):
                published_date = datetime.now()
            article = Article(
                title=entry.title,
                url=entry.link,
                published_date=published_date  # Convert time_struct to datetime
            )
            db.session.add(article)
            db.session.flush()  # Flush the session to assign an ID to the article     
        # Add the Article instance to the entry
        entry['article'] = article
        entries.append(entry)
        articles.append(article)
        # Check if the article is in the database
        #article_in_db = Article.query.get(article.id)
        #if article_in_db is not None:
            #print(f"Article is in the database: {article_in_db.id}")  # Debug statement
        #else:
            #print(f"Article is not in the database: {article.id}")  # Debug statemente database
    db.session.commit()

    # Sort the entries based on their published time
    entries = sorted(entries, key=lambda e: parse(e.dc_date) if hasattr(e, 'dc_date') else datetime.now(), reverse=True)
    return render_template('All_agencies/日テレNEWS_NNN.html', feed=feed.entries[:20],articles=articles)

@app.route("/Al_Jazeera")
@cache.cached(timeout=900)
def Al_Jazeera():
    feed = feedparser.parse('https://www.aljazeera.com/xml/rss/all.xml')
    entries = []  # Initialize the entries list
    articles = []  # Initialize the articles list
    for entry in feed.entries:
        response = requests.get(entry.link)
        soup = BeautifulSoup(response.text, 'html.parser')
        script_tags = soup.find_all('script', type='application/ld+json')
        for script_tag in script_tags:
            data = json.loads(script_tag.string)
            if 'image' in data and isinstance(data['image'], list) and 'url' in data['image'][0]:
                entry['image_url'] = data['image'][0]['url']
                break  # Break the loop as soon as an image URL is found
        else:
            # This will only be executed if the loop didn't break, i.e., no image URL was found
            entry['image_url'] = 'https://encrypted-tbn0.gstatic.com/images?q=tbn:ANd9GcQ9sXvoSpXCYsdA1qW74Q3uGp8CAN19bUWGoQ&s'
        entry['summary'] = html.unescape(entry['summary'])
        # Check if an article with the same URL already exists in the database
        article = Article.query.filter_by(url=entry.link).first()
        # If not, create a new Article instance and add it to the database
        if article is None:
            if 'published_parsed' in entry:
                published_date = datetime(*entry.published_parsed[:6])  # Convert time_struct to datetime
            else:
                published_date = datetime.now()
            article = Article(
                title=entry.title,
                url=entry.link,
                published_date=published_date  
            )
            db.session.add(article)
            db.session.flush()  # Flush the session to assign an ID to the article
        # Add the Article instance to the entry
        entry['article'] = article
        entries.append(entry)
        articles.append(article)
    db.session.commit()
    return render_template('All_agencies/Al_Jazeera.html', feed=feed.entries,articles=articles)

#creating or retrieving an Article instance for a given entry to avoid redudency.
def get_article(entry, feed_url):
    url = entry.link if 'link' in entry else entry.path
    article = Article.query.filter_by(url=url).first()
    if article is None:
        if 'published_parsed' in entry:
            published_date = datetime(*entry.published_parsed[:6])  # Convert time_struct to datetime
        else:
            published_date = datetime.now()
        article = Article(
            title=entry.title,
            url=url,
            published_date=published_date  
        )
        db.session.add(article)
        db.session.flush()  # Flush the session to assign an ID to the article
    return article

@app.route("/SDGs")
@cache.cached(timeout=900)
def SDGs():
    feeds = [
        'https://news.un.org/feed/subscribe/en/news/topic/sdgs/feed/rss.xml',
        'https://www.un.org/sustainabledevelopment/feed/',
        'https://www.unep.org/news-and-stories/rss.xml'
    ]
    entries = []
    for feed_url in feeds:
        feed = feedparser.parse(feed_url)
        for entry in feed.entries[:10]:
            if 'news.un.org' in feed_url:
                image_url = entry.links[1].href if len(entry.links) > 1 else None
                summary = entry.summary_detail.value if 'summary_detail' in entry else None
                original_link = entry.link
                publish_date = entry.published if 'published' in entry else None
            elif 'www.unep.org' in feed_url:  # added condition for new feed
                image_url = entry.field_article_billboard_image
                original_link = entry.path
                if 'field_body' in entry:
                    words = entry.field_body.split()
                    summary = ' '.join(words[:60]) + '...' if len(words) > 60 else entry.field_body
                else:
                    summary = None
                publish_date = entry.created if 'created' in entry else None
            else:
                try:
                    response = requests.get(entry.link, timeout=30)
                    soup = BeautifulSoup(response.content, 'html.parser')
                    div = soup.find('div', class_='story-media')
                    if div:
                        picture = div.find('picture')
                        if picture:
                            img = picture.find('img')
                            image_url = img['src'] if img else None
                        else:
                            image_url = None
                    else:
                        image_url = None
                except Timeout:
                    print("The request timed out")
                    image_url = None
                summary = entry.summary_detail.value if 'summary_detail' in entry else None
                publish_date = entry.published if 'published' in entry else None
                original_link = entry.link
            entry['image_url'] = image_url
            entry['summary'] = summary
            if publish_date:
                try:
                    publish_date = parse(publish_date)
                    if publish_date.tzinfo is not None:  # if datetime object is offset-aware
                        publish_date = publish_date.astimezone(tzutc()).replace(tzinfo=None)  # convert to offset-naive
                except ValueError:
                    print(f"Could not parse date: {publish_date}")
                    publish_date = None
            entry['publish_date'] = publish_date
            entry['original_link'] = original_link  # store the original link in the entry
            entries.append(entry)
            article = get_article(entry, feed_url)
            entry['article'] = article
            # Check if the article is in the database
            #article_in_db = Article.query.get(article.id)
            #if article_in_db is not None:
                #print(f"Article is in the database: {article_in_db.id}")  # Debug statement
            #else:
                #print(f"Article is not in the database: {article.id}")  # Debug statement
    db.session.commit()
    # Sort the entries based on their published time
    entries = sorted(entries, key=lambda e: (e['publish_date'] is None, e['publish_date']), reverse=True)
    return render_template('All_contents/SDGs.html', entries=entries)

@app.route("/Politics")
@cache.cached(timeout=900)
def Politics():
    feeds = [
        'http://feeds.bbci.co.uk/news/politics/rss.xml',
        'https://www.theguardian.com/politics/rss',
        'https://rss.politico.com/politics-news.xml',
        'http://rss.politico.com/defense.xml'
    ]
    entries = []
    articles = []
    for feed_url in feeds:
        feed = feedparser.parse(feed_url)
        for i, entry in enumerate(feed.entries):
            if i < 8:  # Only process the first 6 entries
                if 'feeds.bbci.co.uk' in feed_url:
                    if 'media_thumbnail' in entry and len(entry['media_thumbnail']) > 0:
                        entry['image_url'] = entry['media_thumbnail'][0]['url']
                    else:
                        entry['image_url'] = 'https://upload.wikimedia.org/wikipedia/commons/thumb/e/ea/BBC_World_News_2022_%28Boxed%29.svg/800px-BBC_World_News_2022_%28Boxed%29.svg.png'
                    response = requests.get(entry.link)
                    soup = BeautifulSoup(response.text, 'html.parser') 
                    # Find the <p> tag with class 'player-text' and extract its text
                    p_tag = soup.find('p', class_='sc-eb7bd5f6-0 fYAfXe')
                    if p_tag is not None:
                        lines = p_tag.get_text(separator='\n').split('\n')
                        entry['summary'] = '\n'.join(lines[:3]) + '...'
                elif 'www.theguardian.com' in feed_url:
                    # Parsing logic for 'www.theguardian.com'
                    if 'media_content' in entry and len(entry['media_content']) > 0:
                        entry['image_url'] = entry['media_content'][0]['url']
                    else:
                        entry['image_url'] = 'https://encrypted-tbn0.gstatic.com/images?q=tbn:ANd9GcQR_E1vW-vT3q3rKzhtxt6MHMezjtmOp3_5dg&s'
                    soup = BeautifulSoup(entry['summary'], 'html.parser')
                    for div in soup.find_all("div", {'class':' dcr-4gwv1z'}):
                        div.decompose()
                    summary_text = soup.get_text()
                    summary_words = summary_text.split()  # Split the text by spaces to get a list of words
                    max_words = 75  # Set your desired maximum number of words
                    entry['summary'] = ' '.join(summary_words[:max_words]) + ',continued ... '
                elif 'rss.politico.com' in feed_url:
                    # Check if the entry has a 'media_content' field
                    if 'media_content' in entry and len(entry['media_content']) > 0:
                        # Get the image URL from the 'media_content' field
                        entry['image_url'] = entry['media_content'][0]['url']
                    else:
                        # Make a GET request to the entry's link
                        response = requests.get(entry.link)
                        soup = BeautifulSoup(response.text, 'html.parser')
                        # Find the first <img> tag with a 'data-lazy-img' attribute
                        img_tag = soup.find('img', attrs={'data-lazy-img': True})
                        if img_tag is not None:
                            # Get the image URL from the 'data-lazy-img' attribute
                            entry['image_url'] = img_tag['data-lazy-img']
                        else:
                            # Use a default image URL if no image URL was found
                            entry['image_url'] = 'https://www.politico.eu/wp-content/themes/politico/assets/images/politico-billboard.png'
                    entry['summary'] = entry.summary
                publish_date = entry.get('published')            
                if publish_date:
                    try:
                        publish_date = parse(publish_date)
                        if publish_date.tzinfo is not None:  # if datetime object is offset-aware
                            publish_date = publish_date.astimezone(tzutc()).replace(tzinfo=None)  # convert to offset-naive
                    except ValueError:
                        print(f"Could not parse date: {publish_date}")
                        publish_date = None
                entry['publish_date'] = publish_date
                entries.append(entry)
                article = get_article(entry, feed_url)
                entry['article'] = article
                # Check if the article is in the database
                #article_in_db = Article.query.get(article.id)
                #if article_in_db is not None:
                    #print(f"Article is in the database: {article_in_db.id}")  # Debug statement
                #else:
                    #print(f"Article is not in the database: {article.id}")  # Debug statement
    db.session.commit()
    # Sort the entries based on their published time
    entries = sorted(entries, key=lambda e: (e['publish_date'] is None, e['publish_date']), reverse=True)
    return render_template('All_contents/Politics.html', entries=entries)

@app.route("/Economy")
@cache.cached(timeout=900)
def Economy():
    feeds = [
        'https://www.economist.com/finance-and-economics/rss.xml',
        'http://rss.politico.com/economy.xml',
        'https://www.nhk.or.jp/rss/news/cat5.xml',
        'https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=20910258'
    ]
    entries = []
    for feed_url in feeds:
        feed = feedparser.parse(feed_url)
        for entry in feed.entries[:8]:
            if 'www.nhk.or.jp' in feed_url:
                response = requests.get(entry.link)
                soup = BeautifulSoup(response.text, 'html.parser')
                entry['image_url'] = 'https://upload.wikimedia.org/wikipedia/commons/4/4c/NHK_logo_2020.svg'
                script_tags = soup.find_all('script', type='application/ld+json')  # Find all script tags with type 'application/ld+json'
                for script_tag in script_tags:
                    data = json.loads(script_tag.string)  # Parse the JSON data
                    if data.get('@type') == 'NewsArticle' and 'image' in data and len(data['image']) > 0:
                        entry['image_url'] = data['image'][0]['url']  # Get the image URL
                        break  # Exit the loop once the image URL is found
            elif 'rss.politico.com' in feed_url:
                # Check if the entry has a 'media_content' field
                if 'media_content' in entry and len(entry['media_content']) > 0:
                    # Get the image URL from the 'media_content' field
                    entry['image_url'] = entry['media_content'][0]['url']
            elif 'search.cnbc.com' in feed_url:
                response = requests.get(entry.link)
                soup = BeautifulSoup(response.text, 'html.parser')
                script_tag = soup.find('script', type='application/ld+json')
                if script_tag:
                    data = json.loads(script_tag.string)
                    entry['image_url'] = data.get('image', {}).get('url', 'https://upload.wikimedia.org/wikipedia/commons/4/4c/CNBC_logo.svg')
                else:
                    entry['image_url'] = 'https://sc.cnbcfm.com/applications/cnbc.com/staticcontent/img/cnbc_logo.gif'
            elif 'www.economist.com' in feed_url:
                response = requests.get(entry.link)
                soup = BeautifulSoup(response.text, 'html.parser')
                script_tag = soup.find('script', type='application/ld+json')
                if script_tag:
                    data = json.loads(script_tag.string)
                    entry['image_url'] = data.get('image', 'https://upload.wikimedia.org/wikipedia/commons/4/4c/Politico_logo.svg')
                else:
                    entry['image_url'] = 'https://upload.wikimedia.org/wikipedia/commons/4/4c/Politico_logo.svg'
            publish_date = entry.get('published')
            if publish_date:
                try:
                    publish_date = parse(publish_date)
                    if publish_date.tzinfo is not None:  # if datetime object is offset-aware
                        publish_date = publish_date.astimezone(tzutc()).replace(tzinfo=None)  # convert to offset-naive
                except ValueError:
                    print(f"Could not parse date: {publish_date}")
                    publish_date = None
            entry['publish_date'] = publish_date
            entries.append(entry)
            article = get_article(entry, feed_url)
            entry['article'] = article
            # Check if the article is in the database
            #article_in_db = Article.query.get(article.id)
            #if article_in_db is not None:
                #print(f"Article is in the database: {article_in_db.id}")  # Debug statement
            #else:
                #print(f"Article is not in the database: {article.id}")  # Debug statement
    db.session.commit()
    # Sort the entries based on their published time
    entries = sorted(entries, key=lambda e: (e['publish_date'] is None, e['publish_date']), reverse=True)
    return render_template('All_contents/Economy.html', entries=entries)

@app.route("/Environment")
@cache.cached(timeout=900)
def Environment():
    feeds = [
        'https://www.theguardian.com/uk/environment/rss',
        'https://insideclimatenews.org/feed/',
        'https://www.enn.com/?layout=ja_teline_v:taggedblog&types[0]=1&format=feed&type=rss'
    ]
    entries = []
    for feed_url in feeds:
        feed = feedparser.parse(feed_url)
        for entry in feed.entries[:10]:
            if 'theguardian.com' in feed_url:
                if 'media_content' in entry and len(entry['media_content']) > 0:
                    entry['image_url'] = entry['media_content'][0]['url']
                else:
                    entry['image_url'] = 'https://encrypted-tbn0.gstatic.com/images?q=tbn:ANd9GcQR_E1vW-vT3q3rKzhtxt6MHMezjtmOp3_5dg&s'
                soup = BeautifulSoup(entry['summary'], 'html.parser')
                for div in soup.find_all("div", {'class':' dcr-4gwv1z'}):
                    div.decompose()
                summary_text = soup.get_text()
                summary_words = summary_text.split()  # Split the text by spaces to get a list of words
                max_words = 75  # Set your desired maximum number of words
                entry['summary'] = ' '.join(summary_words[:max_words]) + ',continued ... '
            elif 'insideclimatenews.org' in feed_url:
                response = requests.get(entry['link'])
                # Parse the HTML content of the page
                soup = BeautifulSoup(response.text, 'html.parser')
                # Find the script tag with type="application/ld+json"
                json_script = soup.find('script', type='application/ld+json')
                if json_script:
                    # Parse the JSON content
                    data = json.loads(json_script.string)
                    # Find the image URL in the JSON data
                    image_url = data.get('@graph', [{}])[0].get('thumbnailUrl')
                    if image_url:
                        # If an image URL is found, use it
                        entry['image_url'] = image_url
                    else:
                        # If no image URL is found, use a default image URL
                        entry['image_url'] = 'https://encrypted-tbn0.gstatic.com/images?q=tbn:ANd9GcRQeKvQEJ5MwuooBe6-7nPSkDtezs7VbncS__YFxOB5Dkqioa-8fZpzEYYLKC9FtsQ1OKM&usqp=CAU'
                else:
                    # If no script tag is found, use a default image URL
                    entry['image_url'] = 'https://encrypted-tbn0.gstatic.com/images?q=tbn:ANd9GcRQeKvQEJ5MwuooBe6-7nPSkDtezs7VbncS__YFxOB5Dkqioa-8fZpzEYYLKC9FtsQ1OKM&usqp=CAU'
            elif 'enn.com' in feed_url:
                # Send a GET request to the article page
                response = requests.get(entry['link'])
                # Parse the HTML content of the page
                soup = BeautifulSoup(response.text, 'html.parser')
                # Find the span tag with itemprop="image"
                image_span = soup.find('span', itemprop='image')
                # Find the img tag within the span tag
                image_tag = image_span.find('img') if image_span else None
                if image_tag and 'src' in image_tag.attrs:
                    # If an image tag is found, use its 'src' attribute as the image URL
                    entry['image_url'] = "https:" + image_tag['src']  # prepend "https:" to the src attribute
                else:
                    # If no image tag is found, use a default image URL
                    entry['image_url'] = 'https://encrypted-tbn0.gstatic.com/images?q=tbn:ANd9GcRQeKvQEJ5MwuooBe6-7nPSkDtezs7VbncS__YFxOB5Dkqioa-8fZpzEYYLKC9FtsQ1OKM&usqp=CAU'
            publish_date = entry.get('published')
            if publish_date:
                try:
                    publish_date = parse(publish_date)
                    if publish_date.tzinfo is not None:  # if datetime object is offset-aware
                        publish_date = publish_date.astimezone(tzutc()).replace(tzinfo=None)  # convert to offset-naive
                except ValueError:
                    print(f"Could not parse date: {publish_date}")
                    publish_date = None
            entry['publish_date'] = publish_date
            entries.append(entry)  # Add this line
            article = get_article(entry, feed_url)
            entry['article'] = article
            # Check if the article is in the database
            #article_in_db = Article.query.get(article.id)
            #if article_in_db is not None:
                #print(f"Article is in the database: {article_in_db.id}")  # Debug statement
            #else:
                #print(f"Article is not in the database: {article.id}")  # Debug statement
    db.session.commit()
    # Sort the entries based on their published time
    entries = sorted(entries, key=lambda e: (e['publish_date'] is None, e['publish_date']), reverse=True)
    return render_template('All_contents/Environment.html', entries=entries)

@app.route("/Science_and_Health")
@cache.cached(timeout=900)
def Science_and_Health():
    feeds = [
        'https://rssfeeds.webmd.com/rss/rss.aspx?RSSSource=RSS_PUBLIC',
        'http://feeds.bbci.co.uk/news/health/rss.xml',
        'https://www.nhk.or.jp/rss/news/cat3.xml'
    ]
    entries = []
    for feed_url in feeds:
        feed = feedparser.parse(feed_url)
        for entry in feed.entries[:10]:
            if 'www.nhk.or.jp' in feed_url:
                response = requests.get(entry.link)
                soup = BeautifulSoup(response.text, 'html.parser')
                entry['image_url'] = 'https://upload.wikimedia.org/wikipedia/commons/4/4c/NHK_logo_2020.svg'
                script_tags = soup.find_all('script', type='application/ld+json')  # Find all script tags with type 'application/ld+json'
                for script_tag in script_tags:
                    data = json.loads(script_tag.string)  # Parse the JSON data
                    if data.get('@type') == 'NewsArticle' and 'image' in data and len(data['image']) > 0:
                        entry['image_url'] = data['image'][0]['url']  # Get the image URL
                        break  # Exit the loop once the image URL is found
            elif 'feeds.bbci.co.uk' in feed_url:
                    if 'media_thumbnail' in entry and len(entry['media_thumbnail']) > 0:
                        entry['image_url'] = entry['media_thumbnail'][0]['url']
                    else:
                        entry['image_url'] = 'https://upload.wikimedia.org/wikipedia/commons/thumb/e/ea/BBC_World_News_2022_%28Boxed%29.svg/800px-BBC_World_News_2022_%28Boxed%29.svg.png'
                    response = requests.get(entry.link)
                    soup = BeautifulSoup(response.text, 'html.parser') 
                    # Find the <p> tag with class 'player-text' and extract its text
                    p_tag = soup.find('p', class_='sc-eb7bd5f6-0 fYAfXe')
                    if p_tag is not None:
                        lines = p_tag.get_text(separator='\n').split('\n')
                        entry['summary'] = '\n'.join(lines[:3]) + '...'
            elif 'rssfeeds.webmd.com' in feed_url: 
                if 'media_content' in entry and len(entry['media_content']) > 0:
                    entry['image_url'] = entry['media_content'][0]['url']
                elif 'img' in entry and 'src' in entry['img']:
                    entry['image_url'] = entry['img']['src']
                else:
                    entry['image_url'] = 'https://upload.wikimedia.org/wikipedia/commons/4/42/WebMD_logo.png'
                
            publish_date = entry.get('published')
            if publish_date:
                try:
                    publish_date = parse(publish_date)
                    if publish_date.tzinfo is not None:  # if datetime object is offset-aware
                        publish_date = publish_date.astimezone(tzutc()).replace(tzinfo=None)  # convert to offset-naive
                except ValueError:
                    print(f"Could not parse date: {publish_date}")
                    publish_date = None
            entry['publish_date'] = publish_date
            entries.append(entry)  # Add this line
            article = get_article(entry, feed_url)
            entry['article'] = article
            # Check if the article is in the database
            #article_in_db = Article.query.get(article.id)
            #if article_in_db is not None:
                #print(f"Article is in the database: {article_in_db.id}")  # Debug statement
            #else:
                #print(f"Article is not in the database: {article.id}")  # Debug statement
    db.session.commit()
    # Sort the entries based on their published time
    entries.sort(key=lambda entry: entry['publish_date'], reverse=True)  # Sort the entries by publish_date in descending order        
    return render_template('All_contents/Science_and_Health.html', entries=entries)

# Create an ASGI app using the WsgiToAsgi adapter
ASGI_app = WsgiToAsgi(app)
print("ASGI app created successfully...")


if __name__ == "__main__":
    from hypercorn.config import Config
    from hypercorn.asyncio import serve

    config = Config()
    config.bind = ["localhost:5000"]  # your desired host and port
    config.lifespan = 'on'
    print("Starting server...")
    asyncio.run(serve(ASGI_app, config))