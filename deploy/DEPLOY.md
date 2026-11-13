# Deploying on the EC2 box

Moving from `python3 app.py` in a terminal to gunicorn under systemd. No
virtualenv: everything installs against the system `python3`.

Set this once per shell so the commands below can be pasted as they are:

```bash
APP_DIR=/opt/claim-review          # wherever the checkout actually is
APP_USER=ec2-user                  # 'ubuntu' on an Ubuntu AMI
cd "$APP_DIR"
```

## 1. Find out what is true now

Guessing any of these wrong loses data or makes the app unreachable, and all
four are one command each.

```bash
pgrep -af "python3 app.py"                      # the running dev server
python3 -c "from config import Config; print(Config.DATABASE_PATH)"   # from $APP_DIR
ls -ld "$APP_DIR" var reviews.db 2>/dev/null    # who owns the runtime files
command -v nginx && sudo ss -ltnp | grep -E ':80|:5000'
```

The second one matters most: that path is the live database, with every user
account, review and rule in it. If the service runs as a different user than
the one that has been running `python3 app.py`, it will not be able to write
to it.

## 2. Back the database up

```bash
python3 -c "
import sqlite3, time
from config import Config
out = '/var/tmp/reviews-%s.db' % time.strftime('%Y%m%d-%H%M')
src = sqlite3.connect('file:%s?mode=ro' % Config.DATABASE_PATH, uri=True)
dst = sqlite3.connect(out)
src.backup(dst)
dst.close(); src.close()
print('backed up to', out)"
ls -lh /var/tmp/reviews-*.db
```

Run from `$APP_DIR` - it imports the app's own config, so it backs up whatever
the app actually uses rather than whatever is named `reviews.db`. A live
`.backup` rather than `cp`: the app may be mid-write, and with WAL a plain copy
can be missing the most recent reviews.

## 3. Update the checkout

`app.py`'s listen address is likely edited in place on this box. That edit is
what the pull will collide with, and it is no longer needed - the address is a
setting now.

```bash
git status --short          # anything listed here is a local edit
git checkout -- app.py      # only if the diff is just the host= line
git pull
```

Check the diff first if `git status` shows anything else.

## 4. Install the dependencies

```bash
sudo pip3 install -r requirements.txt
/usr/bin/python3 -m gunicorn --version
```

If pip refuses with "externally-managed-environment", add
`--break-system-packages`. If `--version` fails, gunicorn went to a different
python3 - `which python3` tells you which one, and `ExecStart` in step 6 needs
that path.

## 5. Fill in .env

```bash
grep -q FLASK_SECRET_KEY .env || python3 -c "import secrets; print('FLASK_SECRET_KEY=' + secrets.token_hex(32))" >> .env
grep -q CLAIM_REVIEW_DEBUG .env || echo 'CLAIM_REVIEW_DEBUG=0' >> .env
```

`FLASK_SECRET_KEY` is what stops a restart signing every reviewer out: without
it the signing key is random per process. Set it once and leave it - changing
it later signs everyone out exactly once.

systemd parses this file itself, and it is stricter than a shell: `KEY=value`
only, no `export`, no backticks, no `$VAR` references, no line continuations.
`.env.example` lists every setting.

### Which way in?

**With nginx** (recommended) reviewers use port 80, gunicorn stays on
loopback, and there is somewhere to terminate TLS later:

```bash
sudo cp deploy/nginx-claim-review.conf /etc/nginx/conf.d/claim-review.conf
sudo nginx -t && sudo systemctl enable --now nginx && sudo systemctl reload nginx
```

Then open **80** in the security group. `.env` needs nothing; the default
`CLAIM_REVIEW_BIND=127.0.0.1:5000` is already right.

**Without nginx**, gunicorn serves reviewers directly:

```bash
echo 'CLAIM_REVIEW_BIND=0.0.0.0:5000' >> .env
```

Then open **5000** in the security group. There is no TLS and no upload limit
on this route; the fetch page posts registration-id CSVs through it.

## 6. Install the service

`deploy/claim-review.service` carries defaults that are probably not yours.
Open it and check three things: `WorkingDirectory` and `EnvironmentFile` point
at `$APP_DIR`, `User`/`Group` are `$APP_USER`, and `ExecStart` names the
python3 from step 4.

```bash
sudo sed -e "s#/opt/claim-review#$APP_DIR#g" -e "s#ec2-user#$APP_USER#g" \
    deploy/claim-review.service > /tmp/claim-review.service
sudo install -m 644 /tmp/claim-review.service /etc/systemd/system/claim-review.service
sudo systemctl daemon-reload
```

Make sure the runtime files belong to the account the service runs as:

```bash
sudo chown -R "$APP_USER:$APP_USER" "$APP_DIR"
```

## 7. Stop the old server, start the new one

Port 5000 can only be held by one of them.

```bash
sudo pkill -f "python3 app.py"; sleep 2; pgrep -af "python3 app.py" || echo "dev server stopped"
sudo systemctl enable --now claim-review
systemctl status claim-review --no-pager
```

## 8. Check it

```bash
systemctl show -p Environment claim-review        # CLAIM_REVIEW_BIND as intended?
sudo ss -ltnp | grep -E ':80|:5000'               # listening where you expect
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:5000/login
journalctl -u claim-review -n 50 --no-pager
```

Then from your own machine, against the instance's public address: sign in,
open a claim, and confirm the reviews that were there before still are. Those
reviews are the real test that step 1 found the right database.

## If it will not start

`journalctl -u claim-review -n 50` says which:

- `203/EXEC` - `ExecStart` path is wrong; redo step 4.
- `Permission denied` on the database or `var/` - redo the `chown` in step 6.
- `Address already in use` - the old dev server is still up; redo step 7.
- Starts, but reviewers get nothing - nothing is listening where they are
  looking. Compare step 8's `ss` output against the security group.

## Rolling back

Nothing in this changes the database, so going back is just stopping the
service:

```bash
sudo systemctl disable --now claim-review
python3 app.py
```
