**Production API for AI-Powered Content Generation**\n
**Overview**\n
This document outlines the architecture and features of the backend API for a production-level, AI-driven content generation application. The API is built with Flask and powered by the Google Gemini 1.5 Flash model. It is designed for a production environment, featuring secure user management, robust API rate limiting, and integrated payment processing through Stripe.\n

**Core Features**\n
Secure User Authentication: Provides endpoints for user registration and login using email and password. Passwords are securely hashed to protect user credentials.\n

AI Content Generation: Leverages the Google Gemini API to produce high-quality, generated text based on user inputs.\n

State-Based Usage Limiting: Enforces both daily and monthly generation limits on a per-user basis. This system is designed to reset automatically, ensuring fair usage and preventing abuse.\n

Prompt Injection Defense: Includes a security mechanism to detect and block a list of common prompt injection keywords. This protects the integrity of the AI model and prevents malicious use.\n

Integrated Billing System: Features a dedicated webhook endpoint to handle events from Stripe, including completed checkout sessions and successful recurring payments. This allows for dynamic updates to user accounts, such as adjusting generation limits based on subscription status.\n

Production-Ready Security: The application is configured with Flask-Talisman to enforce Content Security Policy (CSP) and other security headers. It also uses Flask-CORS for managing cross-origin resource sharing in a production environment.\n

**Technical Architecture**\n
Framework: The application is built on Flask, a lightweight and flexible Python web framework. It utilizes extensions like Flask-Login for session management and Flask-MongoEngine for database interaction.\n

Database: MongoDB is used as the primary database for storing user data, including encrypted credentials and usage statistics.\n

AI Integration: The service is directly integrated with Google's Generative AI via the google-generativeai library, specifically using the gemini-1.5-flash model.\n

Payment Processing: All billing and subscription management is handled through Stripe's API and webhooks.\n

**Environment Configuration**\n
For deployment, the application relies on a set of environment variables for secure and proper functioning. These should be configured in the production environment:\n

GEMINI_API_KEY: Your API key for the Google Gemini service.\n

MONGO_URI: The connection URI for your production MongoDB database.\n

SECRET_KEY: A long, random, and secret string used for signing session cookies and other security-related functions.\n

STRIPE_API_KEY: Your secret API key for authenticating with the Stripe API.\n

STRIPE_WEBHOOK_SECRET: The secret key for verifying incoming webhooks from Stripe, ensuring they are authentic.\n

FRONTEND_BASE_URL: The base URL of the frontend application that will be consuming this API.\n

RENDER_EXTERNAL_URL: The public-facing URL of this backend API, used for CORS configuration.\n