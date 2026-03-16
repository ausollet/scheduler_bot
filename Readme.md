# AI Calendar Assistant

An AI-powered calendar assistant that allows users to **schedule, reschedule, search, and cancel meetings using natural language**.  
The system interprets user requests and integrates with **Google Calendar API** to manage events.

The assistant supports conversational interactions such as:

- Scheduling meetings
- Handling conflicts
- Rescheduling existing meetings (Bugfix)
- Cancelling meetings
- Querying calendar events
- Supports multiple agentic AI models with context carryover

---

# Features

## Natural Language Scheduling
Users can schedule meetings using natural language.

Example:

> Schedule a meeting tomorrow at 3 PM for 1 hour.

---

## Smart Rescheduling
The assistant supports flexible rescheduling instructions:

- Move meeting **after another event**
- Move meeting **before another event**
- Schedule **between two meetings**
- Reschedule to a **specific date or time**

---

## Conflict Detection
Before creating an event, the system checks calendar availability using free/busy queries.

---

## Conversation State Management
The assistant tracks conversation state to handle multi-step interactions such as:

- Deciding which meeting to reschedule
- Selecting an available slot
- Confirming event updates

---

## Timezone Awareness
The system ensures meetings are scheduled correctly based on the user's timezone.

---

# Architecture

```
User
 ↓
Prompt Processing
 ↓
Intent & Slot Extraction
 ↓
Conversation State Manager
 ↓
Calendar Service
 ↓
Google Calendar API
```

---

# Project Structure

```
project/
│
├── main.py
├── conversation.py
├── calendar_service.py
├── google_oauth.py
├── llm_client.py
│
├── requirements.txt
└── README.md
```

---

# Deployment

The application is deployed using **Railway**.

Railway automatically builds and deploys the service from the repository.

---

# Environment Variables

Configure the following environment variables in Railway:

```
GOOGLE_APPLICATION_CREDENTIALS=<service-account-json>
GOOGLE_OAUTH_CLIENT_SECRETS=<client-secrets-json>
CALENDAR_ID=primary
GEMINI_API_KEY=<gemini-api-key>

```

---

# Google Setup

1. Create a project in **Google Cloud Console**
2. Enable **Google Calendar API**
3. Create a **Service Account**
4. Download the service account JSON
5. Set the JSON contents in the `GOOGLE_APPLICATION_CREDENTIALS` environment variable
6. Similarly download the oauth client secrets to the `GOOGLE_OAUTH_CLIENT_SECRETS` environment variable

## 1. Create OAuth Credentials

1. Go to Google Cloud Console  
2. Navigate to **APIs & Services → Credentials**
3. Click **Create Credentials → OAuth Client ID**
4. Choose **Web Application**

---

## 2. Configure Authorized Redirect URI

Add the following redirect URI.

For Railway deployment:

```
https://<public_domain>/api/oauth2callback'
```

For local development either download the desktop client secrets or set as:

```
http://localhost:8000/api/oauth2callback
```

---

---

# Local Development

## Install dependencies

```
pip install -r requirements.txt
```

## Run locally

```
python main.py
```

The API server will start locally.

---

# Example Conversations

### Schedule Meeting

User:

```
Schedule a meeting tomorrow at 3 PM for 1 hour
```

Assistant:

```
Meeting scheduled successfully.
```

---

### Cancel Meeting

User:

```
Cancel my marketing meeting tomorrow
```

Assistant:

```
The meeting has been cancelled.
```

---

### Find Meetings

User:

```
Show me my meetings for tomorrow
```

Assistant:

```
The meeting has been cancelled.
```

---

### Reschedule Meeting

User:

```
Move my project meeting after the client call
```

Assistant:

```
Your meeting has been rescheduled to 6 PM.
```

---

# API Endpoints

### Chat Endpoint

Handles conversational scheduling.

```
POST /chat
```

Example Request:

```json
{
  "message": "Schedule a meeting tomorrow at 3 PM"
}
```

Example Response:

```json
{
  "response": "Meeting scheduled successfully"
}
```

---

# Key Design Decisions

### Intent Extraction
User messages are processed to extract structured information such as:

- meeting title
- date
- time
- duration
---

### Availability Checking
The system checks Google Calendar availability before creating events to prevent scheduling conflicts.

---

### Stateful Conversations
Conversation state allows the assistant to handle multi-step workflows like selecting a meeting to reschedule.

---

# Recent Improvements

- Improved timezone handling
- Added **meeting search functionality**
- Improved slot matching during user selection

---

# Future Improvements

Possible enhancements include:

- Multi-user support
- Smart meeting suggestions
- Automatic meeting summaries
- Flawless rescheduling
- Multiple choice deletion

---

# License

MIT License