import os

from claimreview import create_app

app = create_app()

if __name__ == "__main__":
    # Debug mode's reloader restarts the process whenever a watched file
    # changes - and a claim fetch runs in a background thread, so a restart
    # kills it mid-download (the run is then recorded as interrupted on the
    # next start). Set CLAIM_REVIEW_DEBUG=0 to run without it while fetching.
    debug = os.environ.get("CLAIM_REVIEW_DEBUG", "1").strip().lower() not in {
        "0", "false", "no", "off",
    }
    app.run(host="127.0.0.1", port=5000, debug=debug, threaded=True)
