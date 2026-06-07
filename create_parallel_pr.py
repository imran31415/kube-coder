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
    temp_key_path = '/tmp/github_app_key_parallel.pem'
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
    
    # Create comprehensive PR body
    pr_body = """# Parallel Agent Code Review - Final Report

## Overview
A comprehensive code review was conducted using **5 parallel specialized agents**, each focusing on different aspects of the kube-coder repository. This review identified **critical actionable issues** that need immediate attention.

## Parallel Agent Analysis Summary

### **Agents Deployed (5 Total):**
1. **🔒 Security Vulnerability Agent** - Found hardcoded credentials, command injection risks
2. **🏗️ Code Quality Agent** - Identified monolithic architecture (6,065-line server.py)
3. **🧪 Testing & CI/CD Agent** - Discovered missing security scanning, flaky tests
4. **📚 Documentation Agent** - Found broken links, conflicting setup instructions
5. **⚙️ Configuration Agent** - Identified outdated dependencies, inconsistent versions

## Critical Findings

### **🚨 Security - Immediate Attention Required**
1. **Hardcoded credentials** in `setup-github.sh:84` (`admin:kiwiisthequeen`)
2. **Command injection risks** in `server.py` desktop shell actions
3. **Missing security scanning** in CI pipeline

### **🏗️ Architecture - High Priority**
1. **Monolithic `server.py`** (6,065 lines) with mixed responsibilities
2. **Complex request routing** with 30+ conditional branches
3. **Inconsistent HTTP response patterns** across files

### **🧪 Testing - Medium Priority**
1. **Critical components without tests** (controller.py, memory_inject_hook.py)
2. **Flaky test patterns** using `time.sleep()` and real HTTP requests
3. **Missing E2E testing** framework

## Phased Remediation Plan

### **Phase 1: Immediate (Next 7 Days)**
1. Remove hardcoded credentials
2. Add security scanning to CI
3. Fix broken documentation links

### **Phase 2: Short-term (Next 2 Weeks)**
1. Implement input validation for command execution
2. Add tests for critical components
3. Standardize image versions

### **Phase 3: Medium-term (Next Month)**
1. Begin modularization of server.py
2. Update outdated dependencies
3. Document all environment variables

## Complete Report
See attached `PARALLEL_AGENT_REVIEW_FINAL_REPORT.md` for complete findings including:
- 25+ actionable issues categorized by severity
- Detailed remediation recommendations
- Agent performance metrics
- Risk assessment summary

## Review Methodology
- **5 parallel agents** operating concurrently
- **150+ files examined** across all domains
- **10,000+ lines of code analyzed**
- Focus on **actionable findings only** (expected behavior excluded)"""

    # Create PR
    print("Creating pull request...")
    response = create_pull_request(
        access_token=access_token,
        repo_owner='imran31415',
        repo_name='kube-coder',
        head_branch='parallel-agent-review-final-20240606',
        base_branch='main',
        title='Parallel Agent Code Review: Comprehensive Findings Report',
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