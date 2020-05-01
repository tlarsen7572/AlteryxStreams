import asyncio
import collections
import datetime
import threading

import AlteryxPythonSDK as Sdk
import xml.etree.ElementTree as Et
import tweepy
from twitter_listener import TwitterListener


class AyxPlugin:
    def __init__(self, n_tool_id: int, alteryx_engine: object, output_anchor_mgr: object):
        # Default properties
        self.n_tool_id: int = n_tool_id
        self.alteryx_engine: Sdk.AlteryxEngine = alteryx_engine
        self.output_anchor_mgr: Sdk.OutputAnchorManager = output_anchor_mgr
        self.label = "Twitter (" + str(n_tool_id) + ")"

        # Custom properties
        self.Output: Sdk.OutputAnchor = None
        self.ConsumerKey: str = ''
        self.ConsumerSecret: str = ''
        self.AccessToken: str = ''
        self.AccessTokenSecret: str = ''
        self.Follow = None
        self.Track = None

    def pi_init(self, str_xml: str):
        xml = Et.fromstring(str_xml)
        self.ConsumerKey = xml.find("ConsumerKey").text if 'ConsumerKey' in str_xml else ''
        self.ConsumerSecret = xml.find("ConsumerSecret").text if 'ConsumerSecret' in str_xml else ''
        self.AccessToken = xml.find("AccessToken").text if 'AccessToken' in str_xml else ''
        self.AccessTokenSecret = xml.find("AccessTokenSecret").text if 'AccessTokenSecret' in str_xml else ''
        if self.ConsumerKey == '' or self.ConsumerSecret == '' or \
                self.AccessToken == '' or self.AccessTokenSecret == '':
            self.display_error_msg('One or more configuration parameters are missing.')

        follow = xml.find("Follow").text if 'Follow' in str_xml else ''
        track = xml.find("Track").text if 'Track' in str_xml else ''
        if follow is not None:
            self.Follow = follow.split(',')
        if track is not None:
            self.Track = track.split(',')
        if self.Follow is None and self.Track is None:
            self.display_error_msg('You must provide either a list of users or a list of keywords to subscribe to')

        # Getting the output anchor from Config.xml by the output connection name
        self.Output = self.output_anchor_mgr.get_output_anchor('Output')

    def pi_add_incoming_connection(self, str_type: str, str_name: str) -> object:
        return IncomingInterface(self)

    def pi_add_outgoing_connection(self, str_name: str) -> bool:
        return True

    def pi_push_all_records(self, n_record_limit: int) -> bool:
        return False

    def pi_close(self, b_has_errors: bool):
        return

    def display_error_msg(self, msg_string: str):
        self.alteryx_engine.output_message(self.n_tool_id, Sdk.EngineMessageType.error, msg_string)

    def display_warning_msg(self, msg_string: str):
        self.alteryx_engine.output_message(self.n_tool_id, Sdk.EngineMessageType.warning, msg_string)

    def display_info_msg(self, msg_string: str):
        self.alteryx_engine.output_message(self.n_tool_id, Sdk.EngineMessageType.info, msg_string)


class IncomingInterface:
    def __init__(self, parent: AyxPlugin):
        # Default properties
        self.parent: AyxPlugin = parent

        # Custom properties
        self.EventField: Sdk.Field = None
        self.Listener = TwitterListener(self._push_tweet, parent.display_error_msg, parent.display_warning_msg)
        self.Stream = None
        self.Info = None
        self.AuthorField = None
        self.TextField = None
        self.CreatedAtField = None
        self.Creator = None
        self.LoopTask = None
        self.TweetQueue = collections.deque()

    def ii_init(self, record_info_in: Sdk.RecordInfo) -> bool:
        self.Info = Sdk.RecordInfo(self.parent.alteryx_engine)
        self.Info.add_field('Author', Sdk.FieldType.v_wstring, 1073741823, 0)
        self.Info.add_field('Text', Sdk.FieldType.v_wstring, 1073741823, 0)
        self.Info.add_field('Created At', Sdk.FieldType.datetime, 19, 0)
        self.Creator = self.Info.construct_record_creator()

        self.EventField = record_info_in.get_field_by_name('Event')
        if self.EventField is None:
            self.parent.display_error_msg("Incoming data source must contain an 'Event' text field that pushes 'Start' and 'End' events")
            return False

        self.parent.Output.init(self.Info)
        return True

    def ii_push_record(self, in_record: Sdk.RecordRef) -> bool:
        self.parent.display_info_msg("received an event")
        event = self.EventField.get_as_string(in_record)
        if event != 'Start':
            if self.Stream is not None:
                self.Stream.disconnect()
                self.parent.display_info_msg("stopped listening for Tweets")
            if self.LoopTask is not None:
                self.LoopTask.cancel()
            return True

        consumer_key = self.parent.alteryx_engine.decrypt_password(self.parent.ConsumerKey)
        consumer_secret = self.parent.alteryx_engine.decrypt_password(self.parent.ConsumerSecret)
        auth = tweepy.OAuthHandler(consumer_key, consumer_secret)
        access_token = self.parent.alteryx_engine.decrypt_password(self.parent.AccessToken)
        access_token_secret = self.parent.alteryx_engine.decrypt_password(self.parent.AccessTokenSecret)
        auth.set_access_token(access_token, access_token_secret)

        self.Stream = tweepy.Stream(auth, self.Listener)

        event_loop = asyncio.get_event_loop()
        self.LoopTask = event_loop.create_task(self._check_queue_async())
        threading.Thread(target=self.LoopTask).start()

        self.Stream.filter(follow=self.parent.Follow, track=self.parent.Track, is_async=True)
        self.parent.display_info_msg("started listening for Tweets")
        return True

    def ii_update_progress(self, d_percent: float):
        # Inform the Alteryx engine of the tool's progress.
        self.parent.alteryx_engine.output_tool_progress(self.parent.n_tool_id, d_percent)

    def ii_close(self):
        self.LoopTask.cancel()
        self.parent.Output.assert_close()
        return

    def _push_tweet(self, author: str, text: str, created_at: datetime.datetime):
        self.parent.display_info_msg("push tweet data onto stack")
        self.TweetQueue.append(TweetData(author, text, created_at))

    async def _check_queue_async(self):
        self.parent.display_info_msg("starting check queue loop")
        try:
            while True:
                try:
                    tweet = self.TweetQueue.popleft()
                    self.parent.display_info_msg("got tweet data from stack")
                    self.Creator.reset()
                    self.parent.display_info_msg("reset creator")
                    self.Info.get_field_by_name('Author').set_from_string(self.Creator, tweet.Author)
                    self.parent.display_info_msg("set author field")
                    self.Info.get_field_by_name('Text').set_from_string(self.Creator, tweet.Text)
                    self.parent.display_info_msg("set text field")
                    self.Info.get_field_by_name('Created At').set_from_string(self.Creator, datetime.datetime.strftime(tweet.CreatedAt, "%Y-%m-%d %H:%M:%S"))
                    self.parent.display_info_msg("set created at field")
                    output = self.Creator.finalize_record()
                    self.parent.display_info_msg("finalize record")
                    self.parent.Output.push_record(output)
                    self.parent.display_info_msg("push record")
                except IndexError:
                    pass
        except asyncio.CancelledError:
            self.parent.display_info_msg("was cancelled")
        self.parent.display_info_msg("ending check queue loop")
        return


class TweetData:
    def __init__(self, author, text, created_at):
        self.Author = author
        self.Text = text
        self.CreatedAt = created_at
