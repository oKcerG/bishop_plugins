###
# Copyright (c) 2006, Ilya Kuznetsov
# Copyright (c) 2008,2012 Kevin Funk
# Copyright (c) 2014-2016 James Lu
# All rights reserved.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are met:
#
#   * Redistributions of source code must retain the above copyright notice,
#     this list of conditions, and the following disclaimer.
#   * Redistributions in binary form must reproduce the above copyright notice,
#     this list of conditions, and the following disclaimer in the
#     documentation and/or other materials provided with the distribution.
#   * Neither the name of the author of this software nor the name of
#     contributors to this software may be used to endorse or promote products
#     derived from this software without specific prior written consent.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
# AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
# IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE
# ARE DISCLAIMED.  IN NO EVENT SHALL THE COPYRIGHT OWNER OR CONTRIBUTORS BE
# LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR
# CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF
# SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS
# INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN
# CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE)
# ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
# POSSIBILITY OF SUCH DAMAGE.

###

from __future__ import unicode_literals
import supybot.utils as utils
from supybot.commands import *
import supybot.conf as conf
import supybot.plugins as plugins
import supybot.ircutils as ircutils
import supybot.callbacks as callbacks
import supybot.world as world
import supybot.log as log
import supybot.ircdb as ircdb

import json
from datetime import datetime, timedelta
import pickle
import humanize

class LastFMDB():
    """
    Holds the database LastFM IDs of all known LastFM IDs.

    This stores users by their bot account first, falling back to their
    ident@host if they are not logged in.
    """

    def __init__(self, *args, **kwargs):
        """
        Loads the existing database, creating a new one in memory if none
        exists.
        """
        self.db = {}
        try:
            with open(filename, 'rb') as f:
               self.db = pickle.load(f)
        except Exception as e:
            log.debug('LastFM: Unable to load database, creating '
                      'a new one: %s', e)

    def flush(self):
        """Exports the database to a file."""
        try:
            with open(filename, 'wb') as f:
                pickle.dump(self.db, f, 2)
        except Exception as e:
            log.warning('LastFM: Unable to write database: %s', e)

    def set(self, prefix, newId):
        """Sets a user ID given the user's prefix."""

        try:  # Try to first look up the caller as a bot account.
            userobj = ircdb.users.getUser(prefix)
        except KeyError:  # If that fails, store them by nick@host.
            user = prefix.split('!', 1)[1]
        else:
            user = userobj.name

        self.db[user] = newId

    def get(self, prefix):
        """Sets a user ID given the user's prefix."""

        try:  # Try to first look up the caller as a bot account.
            userobj = ircdb.users.getUser(prefix)
        except KeyError:  # If that fails, store them by nick@host.
            user = prefix.split('!', 1)[1]
        else:
            user = userobj.name

        # Automatically returns None if entry does not exist
        return self.db.get(user)

class LastFM(callbacks.Plugin):
    threaded = True

    def __init__(self, irc):
        self.__parent = super(LastFM, self)
        self.__parent.__init__(irc)
        self.db = LastFMDB(filename)
        world.flushers.append(self.db.flush)

        # 2.0 API (see http://www.lastfm.de/api/intro)
        self.APIURL = "http://ws.audioscrobbler.com/2.0/?"

    def die(self):
        world.flushers.remove(self.db.flush)
        self.db.flush()
        self.__parent.die()

    @wrap([optional("something")])
    def np(self, irc, msg, args, user):
        """[<user>]

        Announces the track currently being played by <user>. If <user>
        is not given, defaults to the LastFM user configured for your
        current nick.
        """
        apiKey = self.registryValue("apiKey")
        if not apiKey:
            irc.error("The API Key is not set. Please set it via "
                      "'config plugins.lastfm.apikey' and reload the plugin. "
                      "You can sign up for an API Key using "
                      "http://www.last.fm/api/account/create", Raise=True)
        user = (user or self.db.get(msg.prefix))
        if not user:
            irc.error("use .set <LastFM username> first.", Raise=True)

        # see http://www.lastfm.de/api/show/user.getrecenttracks
        url = "%sapi_key=%s&method=user.getrecenttracks&user=%s&format=json" % (self.APIURL, apiKey, user)
        try:
            f = utils.web.getUrl(url).decode("utf-8")
        except utils.web.Error:
            irc.error("Unknown user %s." % user, Raise=True)
        self.log.debug("LastFM.nowPlaying: url %s", url)

        try:
            data = json.loads(f)["recenttracks"]
        except KeyError:
            irc.error("Unknown user %s." % user, Raise=True)

        user = data["@attr"]["user"]
        tracks = data["track"]

        # Work with only the first track.
        try:
            trackdata = tracks[0]
        except IndexError:
            irc.error("%s doesn't seem to have listened to anything." % user, Raise=True)

        artist = trackdata["artist"]["#text"].strip()  # Artist name
        track = trackdata["name"].strip()  # Track name
        # Album name (may or may not be present)
        album = trackdata["album"]["#text"].strip()
        if album:
            album = ircutils.bold("[%s]" % album)

        try:
            time = int(trackdata["date"]["uts"])  # Time of last listen
            # Format this using the preferred time format.
            tformat = conf.supybot.reply.format.time()
            #time = "at %s" % datetime.fromtimestamp(time).strftime(tformat)
            time = "(%s)" % humanize.naturaltime(datetime.now() - datetime.fromtimestamp(time))
        except KeyError:  # Nothing given by the API?
            time = "(now)"

        public_url = ''
        # If the DDG plugin from this repository is loaded, we can integrate
        # that by finding a YouTube link for the track.
        if self.registryValue("fetchYouTubeLink"):
            ddg = irc.getCallback("DDG")
            if ddg:
                # Each valid result has a preceding heading in the format
                # '<td valign="top">1.&nbsp;</td>', etc.
                try:
                    search = [td for td in ddg._ddgurl('site:youtube.com "%s - %s"' % (artist, track))
                              if "1." in td.text]
                    res = search[0].next_sibling.next_sibling
                    public_url = format('%u', res.a.get('href'))
                except:
                    # If something breaks, log the error but don't cause the
                    # entire np request to fail.
                    log.exception("LastFM: failed to get YouTube link for track %s - %s", artist, track)
        if time == "(now)":
            s = 'is listenning to %s by %s %s %s' % (ircutils.bold(track),ircutils.bold(artist), album, public_url)
        else:
            s = 'listened to %s by %s %s %s %s' % (ircutils.bold(track),ircutils.bold(artist), album, time, public_url)
            
        irc.reply(utils.str.normalizeWhitespace(s))

#    @wrap([optional("something")])
    @wrap       
    def wp(self, irc, msg, args):
        """[<user>]

        Announces the track currently being played by <user>. If <user>
        is not given, defaults to the LastFM user configured for your
        current nick.
        """
        apiKey = self.registryValue("apiKey")
        if not apiKey:
            irc.error("The API Key is not set. Please set it via "
                      "'config plugins.lastfm.apikey' and reload the plugin. "
                      "You can sign up for an API Key using "
                      "http://www.last.fm/api/account/create", Raise=True)
                      
        channel = msg.args[0]
        L = list(irc.state.channels[channel].users)
  
        for nick in L:
            hostmask = irc.state.nickToHostmask(nick)
            #irc.reply(hostmask, prefixNick=False)

            user = self.db.get(hostmask)        
    
            if user:
            # see http://www.lastfm.de/api/show/user.getrecenttracks
                url = "%sapi_key=%s&method=user.getrecenttracks&user=%s&format=json" % (self.APIURL, apiKey, user)
                try:
                    f = utils.web.getUrl(url).decode("utf-8")
                except utils.web.Error:
                    irc.error("Unknown user %s." % user, Raise=True)
                self.log.debug("LastFM.nowPlaying: url %s", url)
        
                try:
                    data = json.loads(f)["recenttracks"]
                except KeyError:
                    irc.error("Unknown user %s." % user, Raise=True)
        
                user = data["@attr"]["user"]
                tracks = data["track"]
        
                # Work with only the first track.
                try:
                    trackdata = tracks[0]
                except IndexError:
                    irc.error("%s doesn't seem to have listened to anything." % user, Raise=True)
        
                artist = trackdata["artist"]["#text"].strip()  # Artist name
                track = trackdata["name"].strip()  # Track name
                # Album name (may or may not be present)
                album = trackdata["album"]["#text"].strip()
                if album:
                    album = ircutils.bold("[%s]" % album)
        
                try:
                    time = int(trackdata["date"]["uts"])  # Time of last listen
                    # Format this using the preferred time format.
                    tformat = conf.supybot.reply.format.time()
                    time = "(%s)" % humanize.naturaltime(datetime.now() - datetime.fromtimestamp(time))
                    # time = ""
                except KeyError:  # Nothing given by the API?
                    time = "(now)"
        
                public_url = ''
                nickquiet = nick[:-1] + '.' + nick[-1:]
                s = '%14s: %s by %s %s %s. %s' % (nickquiet, ircutils.bold(track),
                    ircutils.bold(artist), album, time, public_url)
                
                irc.reply(utils.str.normalizeWhitespace(s),prefixNick=False)
                # irc.reply(s,prefixNick=False)
        
    @wrap(["something"])
    def set(self, irc, msg, args, newId):
        """<user>

        Sets the LastFM username for the caller and saves it in a database.
        """

        self.db.set(msg.prefix, newId)
        irc.reply("you are now http://www.last.fm/user/%s" % newId)

    @wrap([optional("something")])
    def profile(self, irc, msg, args, user):
        """[<user>]

        Prints the profile info for the specified LastFM user. If <user>
        is not given, defaults to the LastFM user configured for your
        current nick.
        """
        apiKey = self.registryValue("apiKey")
        if not apiKey:
            irc.error("The API Key is not set. Please set it via "
                      "'config plugins.lastfm.apikey' and reload the plugin. "
                      "You can sign up for an API Key using "
                      "http://www.last.fm/api/account/create", Raise=True)
        user = (user or self.db.get(msg.prefix) or msg.nick)

        url = "%sapi_key=%s&method=user.getInfo&user=%s&format=json" % (self.APIURL, apiKey, user)
        self.log.debug("LastFM.profile: url %s", url)
        try:
            f = utils.web.getUrl(url).decode("utf-8")
        except utils.web.Error:
            irc.error("Unknown user '%s'." % user, Raise=True)

        data = json.loads(f)
        keys = ("realname", "age", "gender", "country", "playcount")
        profile = {"id": ircutils.bold(user)}
        for tag in keys:
            try:
                s = data["user"][tag].strip() or "N/A"
            except KeyError: # empty field
                s = "N/A"
            finally:
                profile[tag] = ircutils.bold(s)
        try:
            # LastFM sends the user registration time as a unix timestamp;
            # Format it using the preferred time format.
            time = int(data["user"]["registered"]["unixtime"])
            tformat = conf.supybot.reply.format.time()
            s = datetime.fromtimestamp(time).strftime(tformat)
        except KeyError:
            s = "N/A"
        finally:
            profile["registered"] = ircutils.bold(s)
        irc.reply("%(id)s (realname: %(realname)s) registered on %(registered)s; age: %(age)s / %(gender)s; "
                  "Country: %(country)s; Tracks played: %(playcount)s" % profile)
                  


filename = conf.supybot.directories.data.dirize("LastFM.db")

Class = LastFM
