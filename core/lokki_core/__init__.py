"""Implementation package for the midstream-abort relay.

`core/server.py` is the entry point and re-exports the public names.
Modules:

    core      paths, run log, input flattening, SSE writing, shared errors
    config    config.yaml plus environment overrides, resolved settings
    renamer   frame identity, request-body building, the terminal frame
    sse       server-sent-events framing
    engine    one upstream request per segment, and the segment run loop
    handler   request guards, listeners, main()
"""
