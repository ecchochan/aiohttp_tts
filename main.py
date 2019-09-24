

import os

# Get one from https://console.cloud.google.com/apis/credentials
os.environ['GOOGLE_APPLICATION_CREDENTIALS'] = '' 

RUN_SERVER_IN_SSL = False
CA_BUNDLE_FILE = 'XXX.ca-bundle'
SSL_CRT_FILE   = 'XXX.crt'
SSL_KEY_FILE   = 'XXX.key'


import socketio
import asyncio
import json
import threading
from six.moves import queue
from google.cloud import speech_v1p1beta1 as speech
from google.cloud.speech_v1p1beta1 import types
from google.protobuf.json_format import MessageToDict

'''
mgr = socketio.AsyncRedisManager('redis://')
sio = socketio.AsyncServer(client_manager=mgr,cors_allowed_origins=[])
'''

sio = socketio.AsyncServer(async_mode='aiohttp', 
                           async_handlers=True)
app = web.Application()
sio.attach(app)




###########################################################################################################
##
##
##
##                                             Basic Web Routes
##
##
##
###########################################################################################################

    
@sio.on('debug')
async def debug(sid, args):
    print('DEBUG::', args)



import sys
import aiohttp_jinja2
aiohttp_jinja2.setup(app,
    loader=jinja2.FileSystemLoader('templates'))

@sio.event
async def connect(sid, environ):
    print('Client connected ' + sid, file=sys.stderr)
    
@sio.event
async def disconnect(sid):
    print('Client disconnected ' + sid, file=sys.stderr)


@aiohttp_jinja2.template('index.html')
async def index(request):
    return dict( phrases_examples=[
                     'testing',
                     'good example',
                 ],
                 industry_naics_code_of_audio=61,
                 audio_topic='Testing'
             )



###########################################################################################################
##
##
##
##                                             Speech-to-text
##
##
##
###########################################################################################################

sessions = {}

def get_session(sid):
    if sid not in sessions:
        sessions[sid] = {}
        
    return sessions[sid]
    

class Transcoder(object):
    """
    Converts audio chunks to text
    """
    def __init__(self, 
                 encoding, 
                 rate, 
                 language, 
                 async_callback,
                 context = "", 
                 model = "command_and_search", # command_and_search
                 single_utterance=False,
                 use_enhanced=False,
                 interim_results=True,
                 metadata={}):
        self.buff = queue.Queue()
        self.encoding = encoding
        self.language = language
        self.rate = rate
        self.transcript = None
        self.model= model
        self.context = context
        self.interim_results = interim_results
        self.single_utterance = single_utterance
        self.async_callback = async_callback
        self.terminated = False
        self.metadata = metadata

    def start(self):
        """Start up streaming speech call"""
        threading.Thread(target=self.process, args=(asyncio.new_event_loop(),)).start()

        
    def response_loop(self, responses):
        """
        Pick up the final result of Speech to text conversion
        """
        for response in responses:
            if not response.results:
                continue
            result = response.results[0]
            if not result.alternatives:
                continue
            transcript = result.alternatives[0].transcript
            
            if result.is_final:
                self.transcript = transcript
                print(transcript)
                
    def process(self, loop):
        """
        Audio stream recognition and result parsing
        """
        #You can add speech contexts for better recognition
        cap_speech_context = types.SpeechContext(**self.context)
        metadata = types.RecognitionMetadata(**self.metadata)
        client = speech.SpeechClient()
        config = types.RecognitionConfig(
            encoding=self.encoding,
            sample_rate_hertz=self.rate,
            language_code=self.language,
            speech_contexts=[cap_speech_context,],
            enable_automatic_punctuation=True,
            model=self.model,
            metadata=metadata
        )
        
        streaming_config = types.StreamingRecognitionConfig(
            config=config,
            interim_results=self.interim_results,
            single_utterance=self.single_utterance)
        audio_generator = self.stream_generator()
        requests = iter(types.StreamingRecognizeRequest(audio_content=content)
                    for content in audio_generator)

        responses = client.streaming_recognize(streaming_config, requests)
        #print('process',type(responses))
        try:
            #print('process')
            for response in responses:
                #print('process received')
                if self.terminated:
                    break
                if not response.results:
                    continue
                result = response.results[0]
                if not result.alternatives:
                    continue
                speechData = MessageToDict(response)
                global_async_worker.add_task(self.async_callback(speechData))
                
                # debug
                transcript = result.alternatives[0].transcript

                print('>>', transcript, "(OK)" if result.is_final else "")
        except Exception as e:
            print('process excepted', e)
            self.start()

    def stream_generator(self):
        while not self.terminated:
            chunk = self.buff.get()
            if chunk is None:
                return
            if self.terminated:
                break
            yield chunk
            continue
            data = [chunk]
            while True:
                try:
                    chunk = self.buff.get(block=False)
                    if chunk is None:
                        return
                    data.append(chunk)
                except queue.Empty:
                    break
            yield b''.join(data)

    def write(self, data):
        """
        Writes data to the buffer
        """
        self.buff.put(data)


async def audio_processor(websocket, path):
    """
    Collects audio from the stream, writes it to buffer and return the output of Google speech to text
    """
    config = await websocket.recv()
    if not isinstance(config, str):
        print("ERROR, no config")
        return
    config = json.loads(config)
    transcoder = Transcoder(
        encoding=config["format"],
        rate=config["rate"],
        language=config["language"]
    )
    transcoder.start()
    while True:
        try:
            data = await websocket.recv()
        except websockets.ConnectionClosed:
            print("Connection closed")
            break
        transcoder.write(data)
        transcoder.closed = False
        if transcoder.transcript:
            print(transcoder.transcript)
            await websocket.send(transcoder.transcript)
            transcoder.transcript = None


@sio.on('startGoogleCloudStream')
async def startGoogleCloudStream(sid, args):
    session = get_session(sid)
    print('startGoogleCloudStream...')
    
    async def async_callback(data):
        await sio.emit('speechData', data, room=sid)
    
    config = args['config']
    metadata = args['metadata']
    
    transcoder = Transcoder(
        encoding=config["format"],
        rate=config["rate"],
        language=config["language"],
        async_callback=async_callback,
        context=config.get("context"),
        use_enhanced=config.get("use_enhanced", False),
        model=config.get("model", "command_and_search"),
        metadata=metadata
        
    )
    session['recognizeStream'] = transcoder
    transcoder.start()
    

@sio.on('endGoogleCloudStream')
async def endGoogleCloudStream(sid, args):
    session = get_session(sid)
    if 'recognizeStream' in session:
        session['recognizeStream'].terminated = True
        
        del session['recognizeStream']


@sio.on('binaryData')
async def binaryData(sid, args):
    session = get_session(sid)
    #print('streaming...')
    if 'recognizeStream' in session:
        session['recognizeStream'].write(args)



    
    
    
    
    
    
    

###########################################################################################################
##
##
##
##                                             Text-to-speech
##
##
##
###########################################################################################################

    
    
from google.cloud import texttospeech

from functools import lru_cache

@lru_cache(maxsize=None)
def tts_work(text, lang="en-US", gender="NEUTRAL", encoding="MP3"):
    synthesis_input = texttospeech.types.SynthesisInput(text=text)

    voice = texttospeech.types.VoiceSelectionParams(
        language_code=lang,
        ssml_gender=getattr(texttospeech.enums.SsmlVoiceGender,gender))

    audio_config = texttospeech.types.AudioConfig(
        audio_encoding=getattr(texttospeech.enums.AudioEncoding,encoding))

    client = texttospeech.TextToSpeechClient()
    response = client.synthesize_speech(synthesis_input, voice, audio_config)
    return response.audio_content

async def tts(request):
    params = request.rel_url.query
    text = params.get('text', "")
    language_code = params.get('lang', "en-US")
    gender = params.get('gender', "NEUTRAL")
    # encoding = params.get('gender', "MP3")
    
    encoding = "MP3"

    return web.Response(body=tts_work(text,
                                      lang=language_code,
                                      gender=gender,
                                      encoding=encoding
                                     ), headers={'content-type': 'audio/mpeg'})


app.router.add_route('GET', '/tts', tts)


    
    


    
    
    
    
    
    

###########################################################################################################
##
##
##
##                                    Async worker in another thread
##
##
##
###########################################################################################################

'''


async def test():
    for x in range(10):
        print("running")
        await asyncio.sleep(1)
        
        
async_worker.add_task( test() )

'''

import time
import asyncio
import functools
from threading import Thread, current_thread, Event
from concurrent.futures import Future

class threaded_async_worker(Thread):
    def __init__(self, start_event):
        Thread.__init__(self)
        self.loop = None
        self.tid = None
        self.event = start_event

    def run(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        self.tid = current_thread() 
        self.loop.call_soon(self.event.set)
        self.loop.run_forever()

    def stop(self):
        self.loop.call_soon_threadsafe(self.loop.stop)

    def add_task(self, coro):
        def _async_add(func, fut):
            try:
                ret = func()
                fut.set_result(ret)
            except Exception as e:
                fut.set_exception(e)

        f = functools.partial(asyncio.ensure_future, coro, loop=self.loop)
        if current_thread() == self.tid:
            return f() # We can call directly if we're not going between threads.
        else:
            # We're in a non-event loop thread so we use a Future
            # to get the task from the event loop thread once
            # it's ready.
            fut = Future()
            self.loop.call_soon_threadsafe(_async_add, f, fut)
            return fut.result()

    def cancel_task(self, task):
        self.loop.call_soon_threadsafe(task.cancel)


event = Event()
global_async_worker = threaded_async_worker(event)
global_async_worker.start()
event.wait() # Let the loop's thread signal us, rather than sleeping









###########################################################################################################
##
##
##
##                                               Start Server
##
##
##
###########################################################################################################




import ssl

if RUN_SERVER_IN_SSL:
    # use HTTP
    ssl_context = ssl.create_default_context()
else:
    # use HTTPS
    ssl_context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH, cafile=CA_BUNDLE_FILE)
    ssl_context.load_cert_chain(SSL_CRT_FILE, SSL_KEY_FILE)

if __name__ == '__main__':
    web.run_app(app, host='0.0.0.0', port=9003, ssl_context=ssl_context)








    
    
