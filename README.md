# pulsar-cli

Live thermal video and settings for a Pulsar Axion Compact XM30F over its Wi-Fi hotspot. Python 3 standard library plus Homebrew `ffmpeg`/`ffplay`.

## Usage

Copy `.env.example` to `.env` and fill in the hotspot name and password. Run `./pulsar.py connect` to join the hotspot. The scope is at `172.28.0.1`.

```sh
./pulsar.py connect                 # join the scope's Wi-Fi hotspot using .env
./pulsar.py play                    # live view in ffplay, Ctrl-C to quit
./pulsar.py zoom                    # show zoom, e.g. 3.0x
./pulsar.py zoom 6.5                # set zoom, 3.0-12.0 in steps of 0.1
./pulsar.py rec start               # start recording to the scope's memory
./pulsar.py rec stop                # stop recording
./pulsar.py rec                     # idle, recording or stopping
./pulsar.py set Brightness 12       # change any writable setting from `schema`
./pulsar.py get Zoom Brightness     # read named settings
./pulsar.py record out.mkv -t 30    # record 30 s, stream copy, no re-encode
./pulsar.py pipe | ffmpeg -i - ...  # MPEG-TS on stdout
./pulsar.py url                     # start the stream and print the RTSP URL
./pulsar.py info                    # model, serial, firmware
./pulsar.py get                     # all current settings
./pulsar.py schema                  # settings with types and ranges
./pulsar.py raw getdeviceinfo       # send any control command
```

`play`, `record` and `pipe` reconnect when video stops. A setting change stops video for about 3 seconds.

Control commands go to port 5005 and fall back to 5006 when 5005 is busy. `play` releases its control connection after starting the stream.

## Protocol

### Control: TCP 5005

- One command per line, terminated by `\r\n`. Lines ending in a bare `\n` get no reply.
- Arguments follow the command after `?` as JSON: `set?{"code":"Zoom","value":4000}`.
- Port 5005 serves one client at a time. Other clients get `rejected` or a closed connection.
- Replies are one JSON object per line, terminated by `\r\n`.
- A JSON request body gets the reply `rejected`.
- Unknown commands return `{"cmd":"<cmd>","error":{"code":9,...,"msg":"command not found"}}`.

Known commands:

| Command | Result |
|---|---|
| `getdeviceinfo` | Model, serial, hardware and software versions, API version |
| `version` | API version (`"3"`) |
| `info` | Settings schema: code, type, read/write mode, range |
| `get` | Current value of every setting |
| `get?["Zoom","Brightness"]` | Current value of the named settings |
| `set?{"code":"Zoom","value":4000}` | Changes one setting and echoes it. Zoom is magnification × 1000 |
| `ping` | Empty acknowledgement |
| `stream_start` | Opens the RTSP server on TCP 554 |
| `stream_stop` | Accepted. The RTSP server keeps serving video afterwards |
| `start_video` | Starts recording to the scope's memory. `RecStatus` reads 1 about 2 s later |
| `stop_video` | Stops recording. Send it on 5005; on 5006 the recording kept running |

Port 5006 uses the same framing and accepts several clients. It always allows `ping`, `version`, `getdeviceinfo`, `stream_start` and `get?[...]`. Plain `get` and `info` return `rejected`. `set?{...}` worked while another client held 5005 and returned `rejected` when 5005 was free.

`RecStatus`: 0 idle, 1 recording, 2 seen briefly after `stop_video`. Writing `RecStatus` does not start or stop a recording. `RecMode`: 0 video, 1 photo.

### Video: RTSP on TCP 554

- URL `rtsp://172.28.0.1/`. Port 554 is closed until `stream_start`.
- One track: H.264 High profile (`profile-level-id=640028`), 528×400, 30 fps, with the scope's on-screen display burned in.
- The SDP sets the RTP clock to 1000 Hz and `Content-Base: (null)`. ffmpeg handles both.
- Use `-rtsp_transport udp`. Interleaved TCP stalls after DESCRIBE.
- Set `-buffer_size 4194304`. The default buffer drops packets.
- Changing a setting stops RTP to every open session, with no BYE or RTSP message. New sessions get video 0.3-1.4 s after the change.
- The server ends a session after 65 s without a keepalive.

### Other ports

- TCP: only 5005, 5006, and 554 once streaming.
- UDP: 67 (DHCP) and 52108. 52108 accepts packets and never replies.

## Device

Axion XM30F, hardware MTV044.003, software 2.0.100, API 3, serial 775212353.

## License

[PolyForm Noncommercial 1.0.0](LICENSE.md). Personal, research, hobby, educational, and nonprofit use are allowed. Commercial use requires a separate license.
