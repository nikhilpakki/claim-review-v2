import os

from claimreview import create_app

app = create_app()

BANNER = """
  ---------------------------------------------------------------------
   This is the DEVELOPMENT server. One process, no request queue, and it
   is not built to be shared. On the EC2 box run it under gunicorn:

       sudo pip3 install -r requirements.txt
       sudo cp deploy/claim-review.service /etc/systemd/system/
       sudo systemctl daemon-reload && sudo systemctl enable --now claim-review

   or, to try it without systemd, from this folder:

       python3 -m gunicorn --worker-class gthread --workers 1 --threads 16 --bind 127.0.0.1:5000 wsgi:app

   The worker count must stay 1 - see wsgi.py.
  ---------------------------------------------------------------------
"""

DEBUG_BANNER = """
  ---------------------------------------------------------------------
   DEBUG MODE IS ON. Two things this does that nobody wants on a shared
   box:

     - the interactive debugger runs any Python it is given, to anyone
       who can reach an unhandled exception;
     - the reloader restarts on any file change, which kills a claim
       fetch mid-download and signs every reviewer out when
       FLASK_SECRET_KEY is unset.

   Set CLAIM_REVIEW_DEBUG=0 to turn it off.
  ---------------------------------------------------------------------
"""

if __name__ == "__main__":
    # Debug mode's reloader restarts the process whenever a watched file
    # changes - and a claim fetch runs in a background thread, so a restart
    # kills it mid-download (the run is then recorded as interrupted on the
    # next start). Set CLAIM_REVIEW_DEBUG=0 to run without it while fetching.
    debug = os.environ.get("CLAIM_REVIEW_DEBUG", "1").strip().lower() not in {
        "0", "false", "no", "off",
    }
    # Printed rather than logged, and printed every time: this was being run
    # as the production server for ten people, which is a thing the program
    # knows and should say.
    print(BANNER)
    if debug:
        print(DEBUG_BANNER)
    if not os.environ.get("FLASK_SECRET_KEY"):
        print("  FLASK_SECRET_KEY is unset, so sessions are signed with a key\n"
              "  that changes on every start: every restart signs everyone\n"
              "  out. Set it in .env.\n")
    # The address was hard-coded to loopback, which meant anyone wanting the
    # app reachable from another machine had to edit this line - a change that
    # then collides with every git pull. Set CLAIM_REVIEW_HOST=0.0.0.0 in .env
    # instead. Loopback stays the default: listening on every interface is a
    # decision about who can reach the box, not a convenience.
    host = os.environ.get("CLAIM_REVIEW_HOST", "127.0.0.1").strip() or "127.0.0.1"
    port = int(os.environ.get("CLAIM_REVIEW_PORT", "5000"))
    if debug and host not in {"127.0.0.1", "localhost", "::1"}:
        print(f"  Listening on {host}, so the debugger above is offered to\n"
              f"  every machine that can reach port {port}. Anyone who can\n"
              "  provoke an error gets a Python prompt on this box. Set\n"
              "  CLAIM_REVIEW_DEBUG=0.\n")
    app.run(host=host, port=port, debug=debug, threaded=True)
