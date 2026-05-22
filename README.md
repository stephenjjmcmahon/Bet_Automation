## Initial Setup

### Prequisite
- Python 3.x

### Steps

1. Clone Repo

2. Create a virtual environment
```bash
   python -m venv venv
```

2.1 Activate it
```bash
   # macOS/Linux
   source venv/bin/activate

   # Windows
   venv\Scripts\activate
```

2.2 Install dependencies
```bash
   pip install -r requirements.txt
```

2.3 Set python interpreter to the correct venv. On VSCode: 
- Ctrl + Shift + P
- Type and select 'Python: Select Interpreter'
- Select the one that shows your venv path, something like .\venv\Scripts\python.exe

3. Create file called '.env' in Bet_Automation folder and copy contents from email into it


## Daily Setup
1. Go to Betfair Exchange Api and follow steps to get the session token. 
- This involves signing in, either use my details (check email) or make new account. My details p easier

2. Paste Session Token into the correct place in the .env file.