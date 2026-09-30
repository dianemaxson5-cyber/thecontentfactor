"""Start the CRM:  python app.py   then open http://localhost:5000"""
import os
import threading
import webbrowser

from crm import create_app

app = create_app()

if __name__ == "__main__":
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "5000"))
    url = f"http://{'localhost' if host == '127.0.0.1' else host}:{port}"
    print(f"\n  Your CRM is running at {url}")
    print("  Keep this window open while you use it. Close it to stop the CRM.\n")
    if host == "127.0.0.1" and not os.environ.get("NO_BROWSER"):
        threading.Timer(1.5, webbrowser.open, [url]).start()
    app.run(host=host, port=port, debug=False, use_reloader=False, threaded=True)
