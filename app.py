"""Start the CRM:  python app.py   then open http://localhost:5000"""
import os

from crm import create_app

app = create_app()

if __name__ == "__main__":
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "5000"))
    print(f"\n  Your CRM is running at http://{'localhost' if host == '127.0.0.1' else host}:{port}\n")
    app.run(host=host, port=port, debug=False, use_reloader=False, threaded=True)
