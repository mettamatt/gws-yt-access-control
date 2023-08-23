import os
from flask import Flask, request
from main import toggle_access

app = Flask(__name__)

app.config['SERVICE_ACCOUNT_FILE'] = os.environ.get('SERVICE_ACCOUNT_FILE')
app.config['ADMIN_EMAIL'] = os.environ.get('ADMIN_EMAIL')
app.config['API_KEY'] = os.environ.get('API_KEY')
app.config['USER_EMAIL'] = os.environ.get('USER_EMAIL')
app.config['UNRESTRICTED_OU'] = os.environ.get('UNRESTRICTED_OU')
app.config['RESTRICTED_OU'] = os.environ.get('RESTRICTED_OU')
app.config['PROJECT_ID'] = os.environ.get('PROJECT_ID')
app.config['LOCATION'] = os.environ.get('LOCATION')

@app.route('/toggle-access', methods=['GET'])
def api_toggle_access():
    return toggle_access(request)