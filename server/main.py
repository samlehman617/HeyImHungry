#!/usr/bin/python3

###################################################
# IMPORTS
###################################################
from flask import Flask, abort, request, jsonify, g, url_for, render_template
from util import prepare_key_value
from flask_sqlalchemy import SQLAlchemy
from flask_httpauth import HTTPBasicAuth
from passlib.apps import custom_app_context as pwd_context
from itsdangerous import (TimedJSONWebSignatureSerializer as Serializer, BadSignature, SignatureExpired)
from flask_sqlalchemy import SQLAlchemy
import json
import os

app = Flask(__name__)

###################################################
# CONFIGURATION
###################################################
app.config['SECRET_KEY'] = 'iuLH@N$piu23jI@#ULVN'
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///db.sqlite'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

app.config['OAUTH_CLIENT_ID'] = 'google'
app.config['OAUTH_CLIENT_SECRET'] = 'thisisthegoogleclientsecret'
app.config['OAUTH_PROJECT_ID'] = 'hey-i-m-hungry'
app.config['AUTH_TOKEN_VALIDITY'] = 180000

###################################################
# MODELS
###################################################

# Connect to the database
db = SQLAlchemy(app)

# User model
class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(32), index=True)
    password_hash = db.Column(db.String(64))
    device_pin = db.Column(db.Integer)
    food_listings = db.relationship('FoodListing', backref='User', lazy=True)

    def hash_password(self, password):
        self.password_hash = pwd_context.encrypt(password)

    def verify_password(self, password):
        return pwd_context.verify(password, self.password_hash)

    def generate_auth_token(self, expiration=app.config['AUTH_TOKEN_VALIDITY']):
        s = Serializer(app.config['SECRET_KEY'], expires_in=expiration)
        return s.dumps({'id': self.id})

    @staticmethod
    def verify_auth_token(token):
        s = Serializer(app.config['SECRET_KEY'])

        try:
            data = s.loads(token)
        except SignatureExpired:
            return None
        except BadSignature:
            return None
        
        user = User.query.get(data['id'])
        return user

# Food listing model
class FoodListing(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    loc_lat = db.Column(db.Float)
    loc_long = db.Column(db.Float)
    user = db.Column(db.Integer, db.ForeignKey('user.id'))
    added = db.DateTime()
    food_items = db.relationship('FoodItem', backref='FoodListing', lazy=True)

# Food item model
class FoodItem(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(60))
    photo = db.Column(db.String(60))
    quantity = db.Column(db.Integer)
    reserved_by = db.Column(db.Integer, db.ForeignKey('foodlisting.id'))

###################################################
# AUTHENTICATION
###################################################

# -------------------------------------------------
# AUTHENTICATION HELPER FUNCTIONS
# -------------------------------------------------

# Verify a username/token and password combo
def verify_password_or_token(username_or_token, password = ''):
    # First try to authenticate by token
    user = User.verify_auth_token(username_or_token)

    if not user:
        # Try to authenticate with username/password
        user = User.query.filter_by(username=username_or_token).first()
        if not user or not user.verify_password(password):
            return False

    return user

# Create new users
def new_user(username, password):
    if username is None or password is None:
        return False
    
    if User.query.filter_by(username=username).first() is not None:
        return False
    
    # Create a new user
    user = User(username=username)
    user.hash_password(password)
    db.session.add(user)
    db.session.commit()

    return user

# Authorized (valid token) decorator
def verify_token(func):
    def wrapper(*args, **kwargs):
        # Make sure the authorization header exists
        if 'Authorization' not in request.headers:
            abort(400)

        # Verify the sent token
        user = verify_password_or_token(request.headers['Authorization'].split(' ')[-1])
        if not user:
            abort(400)

        func()

    return wrapper

# Verify OAuth requests by matching the client ID
def oauth_verify_client_id(func):
    def wrapper(*args, **kwargs):
        # Make sure the client ID is correct
        if request.args.get('client_id', '') != app.config['OAUTH_CLIENT_ID']:
            abort(400)

        func()

    return wrapper

# -------------------------------------------------
# AUTHENTICATION FUNCTIONALITY
# -------------------------------------------------

# Login via the simple API and get an authorization token
@app.route('/api/login')
def simple_login():
    user = verify_password_or_token(request.json.get('username'), request.json.get('password'))
    if not user:
        abort(400)

    # Generate a new authorization token
    auth_token = user.generate_auth_token().decode('ascii')

    return jsonify({'token': auth_token, 'duration': app.config['AUTH_TOKEN_VALIDITY']}, 200)

# Create an account via the simple API
@app.route('/api/users', methods=['POST'])
def simple_new_user():
    user = new_user(request.json.get('username'), request.json.get('password'))
    if not user:
        abort(400)

    return (jsonify({'username': user.username}), 201, {'Location': url_for('simple_login')})

# Show OAuth login page
@oauth_verify_client_id
@app.route('/oauth/login')
def oauth_login_view():
    return render_template('login.html')

# Handle login via OAuth and get an authorization token
@oauth_verify_client_id
@app.route('/oauth/login', methods=['POST'])
def oauth_login():
    # Verify the password is correct, if so, generate an authorization token and redirect
    user = verify_password_or_token(request.form['username'], request.form['password'])
    if not user:
        return ('Invalid username or password. Please try again.', 400, {'Location': url_for('oauth_login_view')})

    # Login was successful, lets verify the redirect URI has the right project ID
    redirect_uri = request.args.get('redirect_uri', '')
    sent_project_id = redirect_uri.split('/')[-1]
    if sent_project_id != app.config['OAUTH_PROJECT_ID']:
        abort(400)

    # Generate a new authorization token
    auth_token = user.generate_auth_token().decode('ascii')

    # Redirect back to requestor
    return redirect(redirect_uri + '?code=' + auth_token + '&state=' + request.args.get('state', ''), code=302)

# Exchange a previously used (but still valid) key for a new key
@oauth_verify_client_id
@app.route('/oauth/exchange', methods=['POST'])
def oauth_exchange_key():
    # Check client secret
    if request.form['client_secret'] != app.config['OAUTH_CLIENT_SECRET']:
        return (jsonify({'error': 'invalid_grant'}), 400)

    # Work out which token we're working with
    if request.form['grant_type'] == 'authorization_code':
        token = request.form['code']
    elif request.form['grant_type'] == 'refresh_token':
        token = request.form['refresh_token']

    # Verify the sent token
    user = verify_password_or_token(token)
    if not user:
        return (jsonify({'error': 'invalid_grant'}), 400)

    # Generate the correct response format
    new_access_token = user.generate_auth_token().decode('ascii')

    resp = {}
    if request.form['grant_type'] == 'authorization_code':
        new_refresh_token = user.generate_auth_token(7889400).decode('ascii') # Refresh token should not expire

        resp = {
            'token_type': 'Bearer',
            'access_token': new_access_token,
            'refresh_token': new_refresh_token,
            'expires_in': app.config['AUTH_TOKEN_VALIDITY']
        }
    elif request.form['grant_type'] == 'refresh_token':
        resp = {
            'token_type': 'Bearer',
            'access_token': new_access_token,
            'expires_in': app.config['AUTH_TOKEN_VALIDITY']
        }

    return (jsonify(resp), 200)

###################################################
# FUNCTIONALITY
###################################################
#@app.route('/api/resource')
#@login_required
#def get_resource():
#    return jsonify({'data': 'Hello, %s!' % g.user.username})

if __name__ == '__main__':
    if not os.path.exists('db.sqlite'):
    	db.create_all()

    app.run(debug=False)