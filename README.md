# The Content Factor CRM

A simple CRM and email marketing tool for a small business. It does the core jobs of HubSpot in one small app that you run yourself:

- **Contacts**: keep every lead, prospect and customer in one place with tags, notes, calls, meetings and a full activity timeline.
- **Deals**: track sales in a simple pipeline (New → Qualified → Proposal → Won/Lost). Winning a deal marks the contact as a customer.
- **Personal emails**: write to one or a few contacts. Each person gets their own copy with their name, company and other details filled in.
- **Campaigns**: send one email to a group, such as all customers or everyone tagged `newsletter`.
- **Nurture sequences**: a series of emails sent automatically over days or weeks. They start when a contact reaches a stage or gets a tag, and stop when the contact becomes a customer or unsubscribes.
- **Content library**: store articles, guides and links, then share them by email. You see who opened what.
- **Tracking**: opens, clicks and content views are logged on each contact's timeline.
- **Sign-up form**: a public page and embed code for your website. New sign-ups become leads and start nurture sequences.
- **Compliance built in**: every marketing email includes your address and a one-click unsubscribe link. Unsubscribed contacts are never emailed.

## Start it

You need Python 3.10 or newer.

```bash
pip install -r requirements.txt
python app.py
```

Open http://localhost:5000. The first visit asks for your business name, your email and a password.

Your data is stored in one file, `data/crm.db`. Back up that file to back up everything. To store it elsewhere, set `CRM_DB=/path/to/crm.db`.

## Send real email

Until you connect an email account, the app runs in **test mode**: emails are saved under *Sent emails* so you can see them, but nothing is delivered.

To send for real, go to **Settings → Email sending** and enter your SMTP details:

| Provider | Server | Port | Security |
|---|---|---|---|
| Gmail / Google Workspace | smtp.gmail.com | 587 | STARTTLS (use an [app password](https://support.google.com/accounts/answer/185833)) |
| Outlook / Microsoft 365 | smtp.office365.com | 587 | STARTTLS |
| Brevo, Mailgun, SendGrid, Amazon SES, Postmark | from your account | 587 | STARTTLS |

A personal mailbox is fine for one-to-one emails and small lists. For campaigns to more than a few hundred people, use an email service such as Brevo or Amazon SES so your messages reach inboxes.

Also fill in your **mailing address** in Settings. Anti-spam laws require it, and it is added to every email footer.

## Writing emails

Write in plain text:

- Leave a blank line between paragraphs.
- `**bold**` makes bold text.
- `[link text](https://example.com)` makes a link. Plain `https://` addresses become links too.
- Personalize with merge fields. Click them in the editor to insert them:

| Field | Becomes |
|---|---|
| `{{first_name}}` | The contact's first name |
| `{{first_name\|there}}` | First name, or "there" if blank |
| `{{last_name}}`, `{{full_name}}`, `{{company}}`, `{{title}}`, `{{email}}` | Contact details |
| `{{business_name}}`, `{{sender_name}}` | Your details from Settings |
| `{{content:your-slug}}` | A tracked link to an item in your content library |

The preview beside the editor shows exactly what the recipient will see.

## Tracking and your web address

Open and click tracking, content pages, the unsubscribe link and the sign-up form all use the **web address of this CRM** (Settings). While the app runs only on your computer (`localhost`), those links work only for you. To track real recipients, run the app on a server with a public address, for example a small VPS or a service like Render or Railway, and set that address in Settings.

To run on a server:

```bash
pip install -r requirements.txt gunicorn
HOST=0.0.0.0 python app.py            # quick start, or:
gunicorn -w 1 --threads 8 -b 0.0.0.0:5000 app:app
```

Use a single worker (`-w 1`): the nurture scheduler runs inside the app process and checks for due emails every minute. Put it behind HTTPS (e.g. Caddy or nginx) so your login is protected.

## Importing contacts

**Contacts → Import CSV** accepts exports from Excel, Google Sheets, Gmail, Outlook or another CRM. The file needs an **Email** column; it also recognizes First name, Last name, Name, Phone, Company, Title, Stage, Source, Tags and Notes. Existing contacts (same email) are updated, not duplicated.

## Development

```bash
pip install -r requirements-dev.txt
python -m pytest
```

Code layout:

| Path | What it does |
|---|---|
| `app.py` | Starts the app |
| `crm/db.py` | SQLite schema and helpers |
| `crm/mailer.py` | Merge fields, formatting, tracking links, SMTP sending |
| `crm/automation.py` | Nurture sequence enrollment and the background sender |
| `crm/views.py` | Logged-in screens |
| `crm/public.py` | Tracking, unsubscribe, shared content and sign-up pages |
| `crm/auth.py` | Owner login |
| `crm/templates/`, `crm/static/` | Pages, styles and a small script |
