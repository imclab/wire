from meinheld import patch
patch.patch_all()
patch.patch_werkzeug()

from flask import Flask, request, session, g, redirect, url_for, abort, \
     render_template, flash

from wire.models.user import User, UserValidationError, \
    Update, UpdateError, UserNotFoundError
from wire.models.message import Message, \
    MessageValidationError
from wire.models.inbox import Inbox
from wire.models.thread import Thread, \
    ThreadError, InvalidRecipients
from wire.models.contacts import Contacts, \
    ContactExistsError, ContactInvalidError
from wire.models.event import Event, EventValidationError,\
    EventNotFoundError, EventCommentError

from wire.utils.auth import Auth, AuthError

from flaskext.markdown import Markdown
from flaskext.uploads import (UploadSet, configure_uploads, IMAGES,
                              UploadNotAllowed)

import logging
from logging import Formatter, FileHandler

import json
import redis
import uuid
import subprocess

from local_settings import *

uploaded_avatars = UploadSet('avatars', IMAGES)
uploaded_images = UploadSet('images', IMAGES)


def config(debug=None):
    if debug:
        print "Debug mode."
        app = Flask(__name__)

    else:
        app = Flask(__name__, static_path='/')

    app.config.from_object(__name__)
    app.config.from_envvar('WIRE_SETTINGS', silent=True)
    configure_uploads(app, uploaded_avatars)
    configure_uploads(app, uploaded_images)
    Markdown(app)

    return app

app = config(debug=DEBUG)

redis_connection = redis.Redis(
    host=app.config['REDIS_HOST'],
    port=app.config['REDIS_PORT'],
    db=app.config['REDIS_DB']
)

if not app.debug:
    file_handler = FileHandler('error.log', encoding="UTF-8")
    file_handler.setLevel(logging.WARNING)
    file_handler.setFormatter(Formatter(
        '%(asctime)s %(levelname)s: %(message)s '
        '[in %(pathname)s:%(funcName)s:%(lineno)d]'
    ))
    app.logger.addHandler(file_handler)
    print "Added log handler."


@app.before_request
def before_request():
    g.logged_in = False
    g.r = redis_connection
    g.auth = Auth(g.r)
    g.user = User(redis=g.r)
    g.GMAPS_KEY = app.config['GMAPS_KEY']
    try:
        if session['logged_in']:
            g.logged_in = True
            g.user.load(session['logged_in'])
            g.inbox = Inbox(user=g.user, redis=g.r)
            g.unread_count = g.inbox.unread_count()
    except KeyError:
        pass
    except UserNotFoundError:
        logout()


@app.after_request
def after_request(response):
    """Closes the database again at the end of the request."""
    session.pop('user', g.auth.user)
    return response


@app.route('/timeline')
def timeline():
    try:
        g.user.username
    except AttributeError:
        abort(401)
    timeline = g.user.timeline
    return render_template('timeline.html',
        timeline=timeline.updates,
        title='Timeline')


@app.route('/mentions')
def mentions():
    try:
        g.user.username
    except AttributeError:
        abort(401)
    timeline = g.user.mentions
    g.user.reset_mentions()
    return render_template('timeline.html',
        timeline=timeline.updates,
        title='Mentions')


@app.route('/user/<string:username>')
def user_updates(username):
    u = User(redis=g.r)
    try:
        u.load_by_username(username)
    except UserNotFoundError:
        abort(404)

    if u.username in g.user.contacts:
        state = 'contact'
    else:
        state = 'nocontact'

    return render_template('timeline.html',
        timeline=u.posted.updates,
        user=u,
        state=state,
        title='%s' % username,
        disable_input=True)


@app.route('/conversation/<int:conversation_id>')
def conversation(conversation_id):
    updates = []
    for key in g.r.lrange('conversation:%s' % conversation_id, 0, -1):
        u = Update(redis=g.r, user=g.user)
        u.load(key)
        updates.append(u)
    return render_template('timeline.html',
        timeline=updates,
        title='Conversation #%s' % conversation_id,
        disable_input=True,
        disable_userbox=True)


@app.route('/post-update', methods=['POST'])
def post_update():
    try:
        g.user.username
    except AttributeError:
        abort(401)

    u = Update(text=request.form['text'], user=g.user, redis=g.r,
        respond=request.form['respond'])
    try:
        u.save()
        flash("Update posted.", 'success')
    except UpdateError:
        pass
    return redirect(url_for('timeline'))


@app.route('/respond/<int:update_id>')
def respond_update(update_id):
    u = Update(redis=g.r, user=g.user)
    u.load(update_id)
    return render_template('respond.html',
        update=u)


@app.route('/delete-update/<int:update_id>')
def delete_update(update_id):
    u = Update(redis=g.r, user=g.user)
    u.load(update_id)
    if u.user.key != g.user.key:
        abort(401)
    u.delete()
    flash("Update deleted.", 'success')
    return redirect(url_for('timeline'))


@app.route('/')
def intro():
    return render_template('intro.html')


@app.route('/developers')
def developers():
    return render_template('developers.html')


@app.route('/inbox')
def inbox():
    i = g.inbox
    i.load_messages()
    if len(i.threads) == 0:
        empty = True
    else:
        empty = False
    return render_template('inbox.html',
        threads=i.threads,
        empty=empty)


@app.route('/inbox/mark-all-read')
def mark_all_read():
    try:
        g.user.username
    except AttributeError:
        abort(401)
    i = g.inbox
    i.load_messages()
    for thread in i.threads:
        thread.reset_unread_count()
    flash('All messages marked read.', 'success')
    return redirect(url_for('inbox'))


@app.route('/thread/<int:thread_id>', methods=['POST', 'GET'])
def view_thread(thread_id):
    if str(thread_id) not in g.user.get_threads():
        abort(401)

    t = Thread(redis=g.r, user=g.user)
    try:
        t.load(thread_id)
        if request.method == "POST":
            if request.form['action'] == 'reply':
                m = Message(redis=g.r, key=False, user=g.user)
                m.update(request.form)
                t.save()
                t.add_message(m)
                m.send()
                t.load(thread_id)
                flash("Reply has been sent.", 'success')
                return redirect(url_for('view_thread', thread_id=t.key))
        return render_template('thread.html',
            messages=t.messages,
            thread=t,
            subject=t.subject)
    except ThreadError:
        abort(404)


@app.route('/send/<string:recipient>')
def send_message_recipient(recipient):
    return send_message(recipient=recipient)


@app.route('/send', methods=['POST', 'GET'])
def send_message(recipient=False):
    try:
        g.user.username
    except AttributeError:
        abort(401)

    t = Thread(redis=g.r, user=g.user)
    m = Message(redis=g.r, key=False, user=g.user)
    if(recipient):
        try:
            t.parse_recipients(recipient)
        except InvalidRecipients:
            pass

    if request.method == 'POST':
        try:
            t.subject = request.form['subject']
            m.update(request.form)
            t.parse_recipients(request.form['recipients'])
            t.encryption = request.form['encryption']
            t.save()
            t.add_message(m)
            m.send()
            flash('Your message has been successfully wired, \
                    and should arrive shortly.', 'success')
            return redirect(url_for('view_thread', thread_id=t.key))
        except MessageValidationError:
            for error in m.validation_errors:
                flash(error, 'error')
        except InvalidRecipients:
            for recipient in t.invalid_recipients:
                flash('%s is not a valid recipient' % recipient, 'error')
    return render_template('forms/message.html',
        new=True,
        message=m,
        thread=t,
        recipients=t.get_form_recipients())


@app.route('/delete-message/<int:message_id>/<int:thread_id>',
    methods=['POST', 'GET'])
def delete_message(message_id, thread_id):
    if request.method == 'POST':
        t = Thread(redis=g.r, user=g.user)
        t.load(thread_id)
        m = Message(redis=g.r, user=g.user, key=message_id)
        m.load()
        if g.r.get('username:%s' % m.sender.username) != g.user.key:
            abort(401)
        t.delete_message(m)
        flash(u'Message deleted', 'success')
        return redirect(url_for('view_thread', thread_id=thread_id))
    else:
        return render_template('confirm.html',
            _message='Are you sure you want to delete this message?',
            _ok=url_for('delete_message', thread_id=thread_id,
                message_id=message_id),
            _cancel=url_for('view_thread', thread_id=thread_id)
        )


@app.route('/thread/<int:thread_id>/mark-read')
def mark_thread_read(thread_id):
    try:
        if str(thread_id) not in g.user.get_threads():
            abort(401)
    except AttributeError:
        abort(401)

    t = Thread(redis=g.r, user=g.user)
    t.load(thread_id)
    t.reset_unread_count()
    abort(200)


@app.route('/unsubscribe-thread/<int:thread_id>', methods=['POST', 'GET'])
def unsubscribe_thread(thread_id):
    try:
        g.user.username
    except AttributeError:
        abort(401)
    if request.method == "POST":
        t = Thread(redis=g.r, user=g.user)
        t.load(thread_id)
        t.unsubscribe()
        flash(u'Unsubscribed from thread.', 'success')
        return redirect(url_for('inbox'))
    else:
        return render_template('confirm.html',
            _message='Are you sure you wish to unsubscribe from this thread?',
            _ok=url_for('unsubscribe_thread', thread_id=thread_id),
            _cancel=url_for('inbox')
        )


@app.route('/delete-thread/<int:thread_id>', methods=['POST', 'GET'])
def del_thread(thread_id):
    try:
        g.user.username
        if str(thread_id) not in g.user.get_threads():
            abort(401)
    except AttributeError:
        abort(401)
    if request.method == "POST":
        t = Thread(redis=g.r, user=g.user)
        t.load(thread_id)
        t.delete()
        flash(u'Deleted thread.', 'success')
        return redirect(url_for('inbox'))
    else:
        return render_template('confirm.html',
            _message='Are you sure you wish to DELETE this thread?',
            _ok=url_for('del_thread', thread_id=thread_id),
            _cancel=url_for('inbox')
        )


@app.route('/add-recipient/<int:thread_id>', methods=['POST', 'GET'])
def add_recipient(thread_id):
    try:
        g.user.username
        if str(thread_id) not in g.user.get_threads():
            abort(401)
    except AttributeError:
        abort(401)
    username = request.form['username']
    if request.form['confirm'] == '1':
        try:
            t = Thread(redis=g.r, user=g.user)
            t.load(thread_id)
            t.parse_recipients(username)
            t.save()
            flash('Added recipient.', 'success')
        except InvalidRecipients:
            flash(u'Invalid recipient.', 'error')
        return redirect(url_for('view_thread', thread_id=thread_id))
    else:
        return render_template('confirm.html',
            _message='Are you sure you wish to add recipient %s to\
 this thread?' % username,
            _ok=url_for('add_recipient', thread_id=thread_id),
            _cancel=url_for('view_thread', thread_id=thread_id),
            _hiddens=[('username', username)]
        )


@app.route('/address-book')
def contacts(async=False):
    try:
        g.user.username
    except AttributeError:
        abort(401)
    if async:
        return json.dumps(g.user.contacts)
    else:
        return render_template('contacts.html',
            contacts=g.user.contacts
        )


@app.route('/async/address-book')
def async_contacts():
    return contacts(async=True)


@app.route('/user/<string:contact>/add')
def add_contact_t(contact):
    return add_contact(contact,
        redirect_url=url_for('user_updates', username=contact))


@app.route('/user/<string:contact>/del')
def del_contact_t(contact):
    return del_contact(contact,
        redirect_url=url_for('user_updates', username=contact))


@app.route('/add-contact/<string:contact>')
def add_contact(contact, redirect_url=None):
    try:
        g.user.username
    except AttributeError:
        abort(401)
    try:
        c = Contacts(redis=g.r, user=g.user)
        c.add(contact)
        flash('Added user "%s" to address book.' % contact, 'success')
    except KeyError:
        flash('No user specified.', 'error')
    except ContactInvalidError:
        flash('User "%s" does not exist.' % contact, 'error')
    except ContactExistsError:
        flash('User "%s" is already in your address book.' % contact, 'error')

    t = g.user.timeline
    t.rebuild()
    if not redirect_url:
        redirect_url = url_for('contacts')
    return redirect(redirect_url)


@app.route('/add-contact', methods=['POST'])
def add_contact_post():
    contact = request.form['username']
    return add_contact(contact)


@app.route('/async/contact/search/<string:part>')
def async_contact_search(part):
    try:
        g.user.username
    except AttributeError:
        abort(401)
    c = Contacts(redis=g.r, user=g.user)
    return json.dumps(c.search(part))


@app.route('/delete-contact/<string:contact>')
def del_contact(contact, redirect_url=None):
    try:
        g.user.username
    except AttributeError:
        abort(401)
    c = Contacts(redis=g.r, user=g.user)
    c.delete(contact)
    t = g.user.timeline
    t.rebuild()
    flash('Deleted contact "%s".' % contact, 'success')
    if not redirect_url:
        redirect_url = url_for('contacts')
    return redirect(redirect_url)


@app.route('/events')
def list_events():
    e = Event(redis=g.r, user=g.user)
    events, count = e.list()
    return render_template('events.html',
        events=events,
        count=count
    )


@app.route('/event/<int:event_id>')
def view_event(event_id):
    e = Event(redis=g.r, user=g.user)
    e.load(event_id)
    return render_template('event.html',
        event=e,
        state=g.user.get_event_state(event_id)
    )


@app.route('/create-event', methods=['POST', 'GET'])
def new_event():
    return save_event(new=True)


@app.route('/edit-event/<int:event_id>', methods=['POST', 'GET'])
def edit_event(event_id):
    return save_event(new=False, event_id=event_id)


def save_event(event_id=False, new=False):
    try:
        g.user.username
    except AttributeError:
        abort(401)

    e = Event(redis=g.r, user=g.user)

    if not new:
        try:
            e.load(event_id)
        except EventNotFoundError:
            abort(404)
        if e.data['creator'] != g.user.username:
            abort(401)

    if request.method == 'POST':
        e.update(request.form)
        try:
            image = request.files.get('image')
            if image:
                try:
                    e.data['image'] = upload_event_image(image)
                    flash("Upload successful.", 'success')
                except UploadNotAllowed:
                    flash("Upload not allowed.", 'error')
            e.save()
            if new:
                flash("Event created.", 'success')
            else:
                flash("Changes saved.", 'success')
            if new:
                return redirect(url_for('view_event', event_id=e.key))
            else:
                return redirect(url_for('edit_event', event_id=e.key))
        except EventValidationError:
            for error in e.validation_errors:
                flash(error, 'error')

    return render_template('forms/event.html',
        new=new,
        event=e
    )


def upload_event_image(image):
    ext = image.filename.split(".")[-1]
    filename = uploaded_images.save(image, name="%s.%s" % (unique_id(), ext))
    path = "%s/%s" % (app.config['UPLOADED_IMAGES_DEST'], filename)
    resize_image(path, 160)
    return filename


@app.route('/event/<int:event_id>/delete', methods=['GET', 'POST'])
def del_event(event_id):
    e = Event(redis=g.r, user=g.user)
    e.load(event_id)

    try:
        if g.user.username != e.data['creator']:
            abort(401)
    except AttributeError:
        abort(401)

    if request.method == "POST":
        e.delete()
        flash('Event deleted.', 'success')
        return redirect(url_for('list_events'))
    else:
        return render_template('confirm.html',
            _message='Are you sure you wish to DELETE this thread?',
            _ok=url_for('del_event', event_id=event_id),
            _cancel=url_for('view_event', event_id=event_id)
        )


@app.route('/event/<int:event_id>/add-comment', methods=['POST'])
def event_add_comment(event_id):
    try:
        g.user.username
    except AttributeError:
        abort(401)

    e = Event(redis=g.r, user=g.user)
    e.load(event_id)
    try:
        e.add_comment(request.form['text'])
        flash("Comment has been added.", 'success')
    except EventCommentError as detail:
        flash(detail, 'error')
    return redirect(url_for('view_event', event_id=event_id) + '#comments')


@app.route('/event/<int:event_id>/reply/<int:update_id>')
def event_add_reply(update_id, event_id):
    u = Update(redis=g.r, user=g.user)
    u.load(update_id)
    return render_template('respond.html',
        update=u,
        event=event_id)


@app.route('/event/<int:event_id>/delete-comment/<int:comment_id>')
def event_del_comment(event_id, comment_id):
    e = Event(redis=g.r, user=g.user)
    e.load(event_id)
    user = e.comment_user(comment_id)
    if user != g.user.key and e.data['creator'] != g.user.username:
        abort(401)

    e.del_comment(comment_id)
    flash("Comment has been deleted.", 'success')
    return redirect(url_for('view_event', event_id=event_id) + '#comments')


@app.route('/event/<int:event_id>/attend')
def event_set_attend(event_id):
    if not g.logged_in:
        abort(401)
    e = Event(redis=g.r, user=g.user)
    e.load(event_id)
    e.set_attending()
    flash("You have been marked as attending.", 'success')
    return redirect(url_for('view_event', event_id=event_id))


@app.route('/event/<int:event_id>/unattend')
def event_set_unattend(event_id):
    if not g.logged_in:
        abort(401)
    e = Event(redis=g.r, user=g.user)
    e.load(event_id)
    e.set_unattending()
    flash("You have been marked as not attending.", 'success')
    return redirect(url_for('view_event', event_id=event_id))


@app.route('/event/<int:event_id>/maybe')
def event_set_maybe(event_id):
    if not g.logged_in:
        abort(401)
    e = Event(redis=g.r, user=g.user)
    e.load(event_id)
    e.set_maybe()
    flash("You have been marked as maybe attending.", 'success')
    return redirect(url_for('view_event', event_id=event_id))


@app.route('/news')
def news_articles():
    return render_template('news.html')


@app.route('/news/<int:entry_id>')
def view_news_article(entry_id):
    return "viewing entry",  entry_id


@app.route('/sign-up', methods=['POST', 'GET'])
def new_user():
    return edit_user(new=True)


@app.route('/edit-profile', methods=['POST', 'GET'])
def edit_user(new=False):
    if not new:
        try:
            g.user.username
        except AttributeError:
            abort(401)

    if new:
        u = User(redis=g.r)
    else:
        u = g.user
    if request.method == 'POST':
        u.update(request.form, new=new)

        try:
            avatar = request.files.get('avatar')
            if avatar:
                try:
                    u.avatar = upload_avatar(avatar)
                    flash("Upload successful.", 'success')
                except UploadNotAllowed:
                    flash("Upload not allowed.", 'error')
            u.save()
            if new:
                flash('"User "%s" created successfully. \
                    You may now log in.' % u.username, 'success')
                return redirect(url_for('intro'))
            else:
                flash('Profile updated.', 'success')
                return redirect(url_for('edit_user'))
        except UserValidationError:
            for error in u.validation_errors:
                flash(error, 'error')

    return render_template('forms/user.html',
        new=new,
        user=u
    )


def upload_avatar(avatar):
    ext = avatar.filename.split(".")[-1]
    filename = uploaded_avatars.save(avatar, name="%s.%s" % (unique_id(), ext))
    path = "%s/%s" % (app.config['UPLOADED_AVATARS_DEST'], filename)
    resize_image(path, 80)
    return filename


def resize_image(path, x, y=False):
    if not y:
        y = x
    args = [
        'convert',
        path,
        '-resize',
        '%sx%s^' % (x, y),
        '-gravity',
        'center',
        '-extent',
        '%sx%s' % (x, y),
        '-quality',
        '100',
        path
    ]
    return subprocess.check_output(args)


def unique_id():
    return hex(uuid.uuid4().time)[2:-1]


@app.route('/login', methods=['POST'])
def login():
    try:
        g.auth.attempt(request.form['username'], request.form['password'])
        session['logged_in'] = g.auth.user.key
        g.user.load(g.auth.user.key)
    except (KeyError, AuthError):
        flash('Incorrect username or password.', 'error')
        return redirect(url_for('intro'))
    flash('Successfully logged in.', 'success')
    return redirect(request.form['uri'])


@app.route('/logout', methods=['POST', 'GET'])
def logout():
    try:
        session.pop('logged_in')
        flash('Logged out.', 'success')
    except KeyError:
        pass
    return redirect(url_for('intro'))


@app.errorhandler(401)
def unauthorized(error):
    return _status(error), 401


@app.errorhandler(404)
def not_found(error):
    return _status(error), 404


@app.errorhandler(500)
def fuckup(error):
    return _status("500: Internal Server Error"), 500


def _status(error):
    status = [x.strip() for x in str(error).split(":")]
    return render_template('status.html',
        _status=status[0],
        _message=status[1]
        )
