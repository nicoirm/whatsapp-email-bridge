#!/usr/bin/python

# Copyright 2015, Axel Angel, under the GPLv3 license.
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

import os, signal
import datetime, sys
import smtplib
import base64
import yaml
import threading
import socket
import time
import asyncore
import atexit
import tempfile
from parse import parse
from email.mime.text import MIMEText
from email.parser import Parser
from email.utils import formatdate
from smtpd import SMTPChannel, SMTPServer
from html2text import html2text

from yowsup.common import YowConstants
from yowsup import env
from yowsup.layers.auth import YowCryptLayer, YowAuthenticationProtocolLayer, \
        AuthError
from yowsup.layers.axolotl import YowAxolotlLayer
from yowsup.layers.coder import YowCoderLayer
from yowsup.layers import YowLayerEvent
from yowsup.layers.interface import YowInterfaceLayer, ProtocolEntityCallback
from yowsup.layers.logger import YowLoggerLayer
from yowsup.layers.network import YowNetworkLayer
from yowsup.layers.protocol_acks import YowAckProtocolLayer
from yowsup.layers.protocol_acks.protocolentities \
        import OutgoingAckProtocolEntity
from yowsup.layers.protocol_media import YowMediaProtocolLayer
from yowsup.layers.protocol_media.protocolentities \
        import ImageDownloadableMediaMessageProtocolEntity
from yowsup.layers.protocol_media.protocolentities \
        import LocationMediaMessageProtocolEntity
from yowsup.layers.protocol_media.protocolentities \
        import VCardMediaMessageProtocolEntity
from yowsup.layers.protocol_media.protocolentities \
        import RequestUploadIqProtocolEntity
from yowsup.layers.protocol_media.mediauploader import MediaUploader
from yowsup.layers.protocol_iq import YowIqProtocolLayer
from yowsup.layers.protocol_messages import YowMessagesProtocolLayer
from yowsup.layers.protocol_messages.protocolentities \
        import TextMessageProtocolEntity
from yowsup.layers.protocol_receipts import YowReceiptProtocolLayer
from yowsup.layers.protocol_receipts.protocolentities \
        import OutgoingReceiptProtocolEntity
from yowsup.layers.protocol_presence import YowPresenceProtocolLayer
from yowsup.layers.stanzaregulator import YowStanzaRegulator
from yowsup.stacks import YowStack, YOWSUP_CORE_LAYERS

config_file = 'whatsapp_config'


class MailLayer(YowInterfaceLayer):
    def __init__(self):
        YowInterfaceLayer.__init__(self)
        self.startInputThread()

    def startInputThread(self):
        print "Starting input thread"
        server = LMTPServer(self, config.get('socket'), None)
        atexit.register(clean_socket)

    @ProtocolEntityCallback("success")
    def onSuccess(self, entity):
        print "<= WhatsApp: Logged in"

    @ProtocolEntityCallback("failure")
    def onFailure(self, entity):
        print "<= WhatsApp: Failure %s" % (entity)

    @ProtocolEntityCallback("notification")
    def onNotification(self, notification):
        print "<= WhatsApp: Notification %s" % (notification)

    @ProtocolEntityCallback("message")
    def onMessage(self, mEntity):
        if not mEntity.isGroupMessage():
            if mEntity.getType() == 'text':
                self.onTextMessage(mEntity)
            elif mEntity.getType() == 'media':
                self.onMediaMessage(mEntity)
        else:
            src = mEntity.getFrom()
            print "<= WhatsApp: <- %s GroupMessage" % (src)

    @ProtocolEntityCallback("receipt")
    def onReceipt(self, entity):
        ack = OutgoingAckProtocolEntity(entity.getId(), "receipt",
                entity.getType(), entity.getFrom())
        self.toLower(ack)

    def sendEmail(self, mEntity, subject, content):
        timestamp = mEntity.getTimestamp()
        srcShort = mEntity.getFrom(full = False)
        replyAddr = config.get('reply').format(srcShort)
        dst = config.get('sendto')

        formattedDate = datetime.datetime.fromtimestamp(timestamp) \
                                         .strftime('%d/%m/%Y %H:%M')
        content2 = "%s\n\nAt %s by %s (%s) isBroadCast=%s" \
                % (content, formattedDate, srcShort, mEntity.getParticipant(),
                    mEntity.isBroadcast())

        msg = MIMEText(content2, 'plain', 'utf-8')
        msg['To'] = "WhatsApp <%s>" % (dst)
        msg['From'] = "%s <%s>" % (srcShort, mEntity.getParticipant())
        msg['Reply-To'] = "%s <%s>" % (mEntity.getParticipant(), replyAddr)
        msg['Subject'] = subject
        msg['Date'] = formatdate(timestamp)

        if config.get('smtp_ssl', False):
            s_class = smtplib.SMTP_SSL
        else:
            s_class = smtplib.SMTP

        s = s_class(config.get('smtp'), config.get('smtp_port', None))

        if config.get('smtp_user', None):
            s.login(config.get('smtp_user'), config.get('smtp_pass'))

        if not config.get('smtp_ssl', False):
            try:
                s.starttls() # Some servers require it, let's try
            except SMTPException:
                print "<= Mail: Server doesn't support STARTTLS"
                if config.get('force_starttls'):
                    raise

        s.sendmail(dst, [dst], msg.as_string())
        s.quit()
        print "=> Mail: %s -> %s" % (replyAddr, dst)

    def onTextMessage(self, mEntity):
        receipt = OutgoingReceiptProtocolEntity(mEntity.getId(),
                mEntity.getFrom())

        src = mEntity.getFrom()
        print("<= WhatsApp: <- %s Message" % (src))

        content = mEntity.getBody()
        self.sendEmail(mEntity, content, content)
        self.toLower(receipt)

    def onMediaMessage(self, mEntity):
        id = mEntity.getId()
        src = mEntity.getFrom()
        tpe = mEntity.getMediaType()
        url = getattr(mEntity, 'url', None)

        print("<= WhatsApp: <- Media %s (%s)" % (tpe, src))

        content = "Received a media of type: %s\n" % (tpe)
        content += "URL: %s\n" % (url)
        content += str(mEntity)
        self.sendEmail(mEntity, "Media: %s" % (tpe), content)

        receipt = OutgoingReceiptProtocolEntity(id, src)
        self.toLower(receipt)


class YowsupMyStack(object):
    def __init__(self, credentials):
        env.CURRENT_ENV = env.S40YowsupEnv()
        layers = (
            MailLayer,
            (YowAuthenticationProtocolLayer, YowMessagesProtocolLayer,
                YowReceiptProtocolLayer, YowAckProtocolLayer,
                YowMediaProtocolLayer, YowIqProtocolLayer,
                YowPresenceProtocolLayer)
            ) + YOWSUP_CORE_LAYERS

        self.stack = YowStack(layers)
        self.stack.setProp(YowAuthenticationProtocolLayer.PROP_CREDENTIALS,
                credentials)
        self.stack.setProp(YowNetworkLayer.PROP_ENDPOINT,
                YowConstants.ENDPOINTS[0])
        self.stack.setProp(YowCoderLayer.PROP_DOMAIN, YowConstants.DOMAIN)
        self.stack.setProp(YowCoderLayer.PROP_RESOURCE,
                env.CURRENT_ENV.getResource())

    def start(self):
        self.stack.broadcastEvent(
                YowLayerEvent(YowNetworkLayer.EVENT_STATE_CONNECT))

        try:
            self.stack.loop()
        except AuthError as e:
            print("Authentication Error: %s" % e.message)


class LMTPChannel(SMTPChannel):
  # LMTP "LHLO" command is routed to the SMTP/ESMTP command
  def smtp_LHLO(self, arg):
    self.smtp_HELO(arg)

  def smtp_EHLO(self, arg):
    self.smtp_HELO(arg)


class LMTPServer(SMTPServer):
    def __init__(self, yowsup, localaddr, remoteaddr):
        # code taken from original SMTPServer code
        self._yowsup = yowsup
        self._localaddr = localaddr
        self._remoteaddr = remoteaddr
        asyncore.dispatcher.__init__(self)
        try:
            self.create_socket(socket.AF_UNIX, socket.SOCK_STREAM)
            # try to re-use a server port if possible
            self.set_reuse_addr()
            self.bind(localaddr)
            self.listen(5)
        except:
            # cleanup asyncore.socket_map before raising
            self.close()
            raise

    def handle_accept(self):
        conn, addr = self.accept()
        channel = LMTPChannel(self, conn, addr)

    def process_message(self, peer, mailfrom, rcpttos, data):
        m = Parser().parsestr(data)
        print "<= Mail: %s -> %s" % (mailfrom, rcpttos)

        try:
            txt = mail_to_txt(m)
        except Exception as e:
            return "501 malformed content: %s" % (str(e))

        for dst in rcpttos:
            try:
                (phone,) = parse(config.get('reply'), dst)
            except TypeError:
                print "malformed dst: %s" % (dst)
                return "501 malformed recipient: %s" % (dst)

            jid = normalizeJid(phone)

            # send text, if any
            if len(txt.strip()) > 0:
                msg = TextMessageProtocolEntity(txt, to = jid)
                print "=> WhatsApp: -> %s" % (jid)
                self._yowsup.toLower(msg)

            # send media that were attached pieces
            if m.is_multipart():
                for pl in getattr(m, '_payload', []):
                    self.handle_forward_media(jid, pl)

    def handle_forward_media(self, jid, pl):
        ct = pl.get('Content-Type', 'None')
        ct1 = ct.split('/', 1)[0]
        iqtp = None
        if ct1 == 'text':
            return # this is the body, probably
        if ct1 == 'image':
            iqtp = RequestUploadIqProtocolEntity.MEDIA_TYPE_IMAGE
        if ct1 == 'audio':
            iqtp = RequestUploadIqProtocolEntity.MEDIA_TYPE_AUDIO
        if ct1 == 'video':
            iqtp = RequestUploadIqProtocolEntity.MEDIA_TYPE_VIDEO
        if ct.startswith('multipart/alternative'): # recursive content
            for pl2 in pl._payload:
                self.handle_forward_media(jid, pl2)
        if iqtp == None:
            print "<= Mail: Skip unsupported attachement type %s" % (ct)
            return

        print "<= Mail: Forward attachement %s" % (ct1)
        data = base64.b64decode(pl.get_payload())
        tmpf = tempfile.NamedTemporaryFile(prefix='whatsapp-upload_',
                delete=False)
        tmpf.write(data)
        tmpf.close()
        fpath = tmpf.name
        # FIXME: need to close the file!

        entity = RequestUploadIqProtocolEntity(iqtp, filePath=fpath)
        def successFn(successEntity, originalEntity):
            return self.onRequestUploadResult(
                    jid, fpath, successEntity, originalEntity)
        def errorFn(errorEntity, originalEntity):
            return self.onRequestUploadError(
                    jid, fpath, errorEntity, originalEntity)

        self._yowsup._sendIq(entity, successFn, errorFn)

    def onRequestUploadResult(self, jid, fpath, successEntity, originalEntity):
        if successEntity.isDuplicate():
            url = successEntity.getUrl()
            ip = successEntity.getIp()
            print "<= WhatsApp: upload duplicate %s, from %s" % (fpath, url)
            self.send_uploaded_media(fpath, jid, url, ip)
        else:
            ownjid = self._yowsup.getOwnJid()
            mediaUploader = MediaUploader(jid, ownjid, fpath,
                                      successEntity.getUrl(),
                                      successEntity.getResumeOffset(),
                                      self.onUploadSuccess,
                                      self.onUploadError,
                                      self.onUploadProgress,
                                      async=False)
            print "<= WhatsApp: start upload %s, into %s" \
                    % (fpath, successEntity.getUrl())
            mediaUploader.start()

    def onUploadSuccess(self, fpath, jid, url):
        print "WhatsApp: -> upload success %s" % (fpath)
        self.send_uploaded_media(fpath, jid, url)

    def onUploadError(self, fpath, jid=None, url=None):
        print "WhatsApp: -> upload failed %s" % (fpath)
        ownjid = self._yowsup.getOwnJid()
        fakeEntity = TextMessageProtocolEntity("", _from = ownjid)
        self._yowsup.sendEmail(fakeEntity, "WhatsApp upload failed",
                "File: %s" % (fpath))

    def onUploadProgress(self, fpath, jid, url, progress):
        print "WhatsApp: -> upload progression %s for %s, %d%%" \
                % (fpath, jid, progress)

    def send_uploaded_media(self, fpath, jid, url, ip = None):
        entity = ImageDownloadableMediaMessageProtocolEntity.fromFilePath(
                fpath, url, ip, jid)
        self._yowsup.toLower(entity)

    def onRequestUploadError(self, jid, fpath, errorEntity, originalEntity):
        print "WhatsApp: -> upload request failed %s" % (fpath)
        self._yowsup.sendEmail(errorEntity, "WhatsApp upload request failed",
                "File: %s" % (fpath))


def mail_to_txt(m):
    if not m.is_multipart():
        # simple case for text/plain
        return m.get_payload()

    else:
        # handle when there are attachements (take first text/plain)
        for pl in m._payload:
            if "text/plain" in pl.get('Content-Type', None):
                return pl.get_payload()
        # otherwise take first text/html
        for pl in m._payload:
            if "text/html" in pl.get('Content-Type', None):
                return html2text(pl.get_payload())
        # otherwise search into recursive message
        for pl in m._payload:
            try:
                if "multipart/alternative" in pl.get('Content-Type', None):
                    return mail_to_txt(pl)
            except:
                continue # continue to next attachment

        raise Exception("No text could be extracted found")

def loadConfig():
    with open(config_file, 'rb') as fd:
        config = yaml.load(fd)
        return config

def normalizeJid(number):
    if '@' in number:
        return number
    elif "-" in number:
        return "%s@g.us" % number

    return "%s@s.whatsapp.net" % number

def clean_socket():
    try:
        os.unlink(config.get('socket'))
    except OSError:
        pass

if __name__ == "__main__":
    print "Parsing config"
    config = loadConfig()

    print "Starting"
    stack = YowsupMyStack((config.get('phone'), config.get('password')))
    print "Connecting"
    try:
        stack.start()
    except KeyboardInterrupt:
        print "Terminated by user"
