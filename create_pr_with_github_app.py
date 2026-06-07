#!/usr/bin/env python3
import os
import json
import time
import jwt
import requests
from pathlib import Path

# Get GitHub App credentials from environment
GITHUB_APP_ID = os.environ.get('GITHUB_APP_ID')
GITHUB_APP_INSTALLATION_ID = os.environ.get('GITHUB_APP_INSTALLATION_ID')
GITHUB_APP_PRIVATE_KEY = os.environ.get('GITHUB_APP_PRIVATE_KEY')

if not all([GITHUB_APP_ID, GITHUB_APP_INSTALLATION_ID, GITHUB_APP_PRIVATE_KEY]):
    print("Missing GitHub App credentials in environment")
    exit(1)

# Create JWT token
def create_jwt_token(app_id, private_key):
    payload = {
        'iat': int(time.time()),
        'exp': int(time.time()) + 600,  # 10 minutes
        'iss': app_id
    }
    
    # Write private key to temp file
    temp_key_path = '/tmp/github_app_key.pem'
    with open(temp_key_path, 'w') as f:
        f.write(private_key)
    
    # Create JWT
    with open(temp_key_path, 'r') as key_file:
        signing_key = key_file.read()
    
    # Generate JWT
    encoded_jwt = jwt.encode(payload, signing_key, algorithm='RS256')
    
    # Clean up temp file
    os.unlink(temp_key_path)
    
    return encoded_jwt

# Get installation access token
def get_installation_access_token(jwt_token, installation_id):
    url = f'https://api.github.com/app/installations/{installation_id}/access_tokens'
    headers = {
        'Authorization': f'Bearer {jwt_token}',
        'Accept': 'application/vnd.github.v3+json'
    }
    
    response = requests.post(url, headers=headers)
    if response.status_code == 201:
        return response.json()['token']
    else:
        print(f"Failed to get installation token: {response.status_code}")
        print(response.text)
        return None

# Create pull request
def create_pull_request(access_token, repo_owner, repo_name, head_branch, base_branch, title, body):
    url = f'https://api.github.com/repos/{repo_owner}/{repo_name}/pulls'
    headers = {
        'Authorization': f'token {access_token}',
        'Accept': 'application/vnd.github.v3+json'
    }
    
    data = {
        'title': title,
        'body': body,
        'head': head_branch,
        'base': base_branch
    }
    
    response = requests.post(url, headers=headers, json=data)
    return response

# Main execution
def main():
    print("Creating GitHub App JWT token...")
    jwt_token = create_jwt_token(GITHUB_APP_ID, GITHUB_APP_PRIVATE_KEY)
    print(f"JWT token created")
    
    print("Getting installation access token...")
    access_token = get_installation_access_token(jwt_token, GITHUB_APP_INSTALLATION_ID)
    if not access_token:
        print("Failed to get access token")
        return
    
    print("Access token obtained")
    
    # Read PR body from file
    pr_body_file = '/home/dev/kube-coder/CODE_REVIEW_FINDINGS_ACTIONABLE.md'
    if not os.path.exists(pr_body_file):
        print(f"PR body file not found: {pr_body_file}")
        return
    
    with open(pr_body_file, 'r') as f:
        pr_body = f.read()
    
    # Create PR
    print("Creating pull request...")
    response = create_pull_request(
        access_token=access_token,
        repo_owner='imran31415',
        repo_name='kube-coder',
        head_branch='code-review-findings-20240606',
        base_branch='main',
        title='Code Review Findings: Actionable Issues Identified',
        body=pr_body
    )
    
    if response.status_code == 201:
        pr_data = response.json()
        print(f"✅ Pull request created successfully!")
        print(f"   PR Number: #{pr_data['number']}")
        print(f"   PR URL: {pr_data['html_url']}")
        print(f"   PR Title: {pr_data['title']}")
    else:
        print(f"❌ Failed to create PR: {response.status_code}")
        print(f"Response: {response.text}")

if __name__ == '__main__':
    main()