#!/bin/bash

# GitHub Setup Script for Dev Container
# This script helps you connect to GitHub from your dev environment

echo "GitHub Setup for Dev Container"
echo "==============================="
echo ""
echo "Choose your preferred method:"
echo "1) Install GitHub CLI (gh) - Recommended"
echo "2) Generate SSH key for GitHub"
echo "3) Both"
echo ""
read -p "Enter choice [1-3]: " choice

POD_NAME=$(kubectl get pods -n coder -l app=ws-imran -o jsonpath='{.items[0].metadata.name}')

case $choice in
    1)
        echo "Installing GitHub CLI..."
        kubectl exec -it $POD_NAME -n coder -c ide -- bash -c '
            # Install GitHub CLI
            curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg | sudo dd of=/usr/share/keyrings/githubcli-archive-keyring.gpg
            echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" | sudo tee /etc/apt/sources.list.d/github-cli.list > /dev/null
            sudo apt update
            sudo apt install gh -y
            
            echo ""
            echo "GitHub CLI installed! Now run:"
            echo "  gh auth login"
            echo ""
            echo "Choose:"
            echo "  - GitHub.com"
            echo "  - HTTPS"
            echo "  - Login with a web browser (or paste token)"
        '
        ;;
    2)
        echo "Generating SSH key..."
        kubectl exec -it $POD_NAME -n coder -c ide -- bash -c '
            # Generate SSH key
            mkdir -p ~/.ssh
            ssh-keygen -t ed25519 -C "imran@scalebase.io" -f ~/.ssh/id_ed25519 -N ""
            
            # Start ssh-agent
            eval "$(ssh-agent -s)"
            ssh-add ~/.ssh/id_ed25519
            
            # Display the public key
            echo ""
            echo "Your SSH public key (copy this to GitHub):"
            echo "=========================================="
            cat ~/.ssh/id_ed25519.pub
            echo "=========================================="
            echo ""
            echo "Add this key to GitHub:"
            echo "1. Go to https://github.com/settings/keys"
            echo "2. Click \"New SSH Key\""
            echo "3. Paste the key above"
            echo ""
            
            # Configure git
            git config --global user.name "Imran Ali"
            git config --global user.email "imran@scalebase.io"
        '
        ;;
    3)
        # Run both options
        $0 <<< "1"
        $0 <<< "2"
        ;;
    *)
        echo "Invalid choice"
        exit 1
        ;;
esac

echo ""
echo "To connect to your dev container and use git:"
echo "  kubectl exec -it $POD_NAME -n coder -c ide -- bash"
echo ""
echo "Or access via terminal:"
echo "  https://imran.dev.scalebase.io/terminal/"
echo "  (login with admin:kiwiisthequeen)"