#!/bin/bash
# Quick run script for Docker

echo "Building and starting Discord IV Bot..."
docker-compose up -d --build

echo ""
echo "Bot started! Check logs with:"
echo "  docker-compose logs -f"
echo ""
echo "Stop the bot with:"
echo "  docker-compose down"
