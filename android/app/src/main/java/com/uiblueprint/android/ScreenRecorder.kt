package com.uiblueprint.android

import android.hardware.display.DisplayManager
import android.hardware.display.VirtualDisplay
import android.media.AudioAttributes
import android.media.AudioFormat
import android.media.AudioRecord
import android.media.MediaCodec
import android.media.MediaCodecInfo
import android.media.MediaFormat
import android.media.MediaMuxer
import android.media.projection.MediaProjection
import android.os.Build
import android.util.Log
import java.io.File
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicBoolean
import java.util.concurrent.atomic.AtomicInteger

/**
 * Records screen video (H264) and — on API >= 29 when [captureAudio] is true — internal playback
 * audio (AAC) into a single MP4 file, using [MediaCodec] encoders and [MediaMuxer].
 *
 * Internal audio capture uses [android.media.AudioPlaybackCaptureConfiguration] built from the
 * supplied [MediaProjection].  The RECORD_AUDIO permission must be held by the caller.
 *
 * If audio initialisation fails at runtime (e.g. the foreground app has set a restrictive capture
 * policy, or the permission is revoked), the recorder falls back to video-only automatically and
 * still produces a valid MP4.
 *
 * Usage:
 * 1. Construct with a live [MediaProjection] and call [start].
 * 2. Call [stop] when the desired duration has elapsed or the projection is revoked.
 * 3. [onStopped] is called on a background thread once the MP4 is fully flushed.
 *    [audioWasEnabled] is `true` only when audio was successfully captured.
 */
class ScreenRecorder(
    private val mediaProjection: MediaProjection,
    private val width: Int,
    private val height: Int,
    private val dpi: Int,
    val outputFile: File,
    captureAudio: Boolean,
    private val onStopped: (audioWasEnabled: Boolean) -> Unit,
) {
    // True only when API >= Q and the caller requested audio.
    private val wantAudio = captureAudio && Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q

    // Set to true once AudioRecord is successfully started.
    private val actualAudioEnabled = AtomicBoolean(false)

    // Muxer state.
    private val muxer = MediaMuxer(outputFile.absolutePath, MediaMuxer.OutputFormat.MUXER_OUTPUT_MPEG_4)
    private val muxerLock = Any()
    private val muxerStarted = AtomicBoolean(false)

    @Volatile private var videoTrackIndex = -1
    @Volatile private var audioTrackIndex = -1

    // Each encoder thread releases one count when it has added its track.  Both threads wait for
    // zero before starting the muxer so that addTrack() is never called after start().
    private val trackAddLatch = CountDownLatch(if (wantAudio) 2 else 1)

    // Tracks how many encoder threads have reached EOS (or failed).  When all are done, the muxer
    // is stopped and onStopped is invoked.
    private val encodersFinished = AtomicInteger(0)
    private val totalEncoders = if (wantAudio) 2 else 1

    private var videoEncoder: MediaCodec? = null
    private var audioEncoder: MediaCodec? = null
    private var audioRecord: AudioRecord? = null
    private var virtualDisplay: VirtualDisplay? = null

    // Signals both encoder threads to begin shutting down.
    private val stopRequested = AtomicBoolean(false)

    // Guards against double-finalization if both threads arrive simultaneously.
    private val finalized = AtomicBoolean(false)

    // -------------------------------------------------------------------------
    // Public API
    // -------------------------------------------------------------------------

    /** Begin recording.  Must be called exactly once. */
    fun start() {
        startVideoEncoder()
        if (wantAudio) startAudioCapture()
    }

    /**
     * Signals the recording to stop.  Returns immediately; [onStopped] is invoked once the MP4
     * is fully written.  Safe to call from any thread.  Idempotent.
     */
    fun stop() {
        if (!stopRequested.compareAndSet(false, true)) return
        try { videoEncoder?.signalEndOfInputStream() } catch (_: Exception) {}
        try { audioRecord?.stop() } catch (_: Exception) {}
    }

    // -------------------------------------------------------------------------
    // Video pipeline
    // -------------------------------------------------------------------------

    private fun startVideoEncoder() {
        val fmt = MediaFormat.createVideoFormat(MediaFormat.MIMETYPE_VIDEO_AVC, width, height).apply {
            setInteger(MediaFormat.KEY_COLOR_FORMAT, MediaCodecInfo.CodecCapabilities.COLOR_FormatSurface)
            setInteger(MediaFormat.KEY_BIT_RATE, VIDEO_BITRATE)
            setInteger(MediaFormat.KEY_FRAME_RATE, VIDEO_FPS)
            setInteger(MediaFormat.KEY_I_FRAME_INTERVAL, 1)
        }
        val enc = MediaCodec.createEncoderByType(MediaFormat.MIMETYPE_VIDEO_AVC)
        enc.configure(fmt, null, null, MediaCodec.CONFIGURE_FLAG_ENCODE)
        val surface = enc.createInputSurface()
        enc.start()
        videoEncoder = enc

        virtualDisplay = mediaProjection.createVirtualDisplay(
            "UIBlueprintCapture", width, height, dpi,
            DisplayManager.VIRTUAL_DISPLAY_FLAG_AUTO_MIRROR,
            surface, null, null,
        )

        Thread({
            runCatching { drainEncoder(enc, isVideo = true) }
                .onFailure { Log.e(TAG, "Video encoder error", it) }
            onEncoderFinished()
        }, "ScreenRecorder-Video").also { it.isDaemon = true; it.start() }
    }

    // -------------------------------------------------------------------------
    // Audio pipeline
    // -------------------------------------------------------------------------

    private fun startAudioCapture() {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.Q) {
            // Caller should have checked API level; release the latch slot and return.
            trackAddLatch.countDown()
            onEncoderFinished()
            return
        }

        fun failGracefully(reason: String, t: Throwable? = null) {
            if (t != null) Log.w(TAG, reason, t) else Log.w(TAG, reason)
            trackAddLatch.countDown()
            onEncoderFinished()
        }

        val captureConfig = try {
            android.media.AudioPlaybackCaptureConfiguration.Builder(mediaProjection)
                .addMatchingUsage(AudioAttributes.USAGE_MEDIA)
                .addMatchingUsage(AudioAttributes.USAGE_GAME)
                .addMatchingUsage(AudioAttributes.USAGE_UNKNOWN)
                .build()
        } catch (e: Exception) {
            failGracefully("AudioPlaybackCaptureConfiguration failed", e); return
        }

        val audioFmt = AudioFormat.Builder()
            .setEncoding(AudioFormat.ENCODING_PCM_16BIT)
            .setSampleRate(AUDIO_SAMPLE_RATE)
            .setChannelMask(AudioFormat.CHANNEL_IN_STEREO)
            .build()

        val minBuf = AudioRecord.getMinBufferSize(
            AUDIO_SAMPLE_RATE, AudioFormat.CHANNEL_IN_STEREO, AudioFormat.ENCODING_PCM_16BIT,
        ).coerceAtLeast(AUDIO_BUFFER_BYTES)

        val record = try {
            AudioRecord.Builder()
                .setAudioPlaybackCaptureConfig(captureConfig)
                .setAudioFormat(audioFmt)
                .setBufferSizeInBytes(minBuf * 4)
                .build()
        } catch (e: Exception) {
            failGracefully("AudioRecord creation failed", e); return
        }

        if (record.state != AudioRecord.STATE_INITIALIZED) {
            record.release()
            failGracefully("AudioRecord not initialized (state=${record.state})"); return
        }

        val encFmt = MediaFormat.createAudioFormat(
            MediaFormat.MIMETYPE_AUDIO_AAC, AUDIO_SAMPLE_RATE, AUDIO_CHANNELS,
        ).apply {
            setInteger(MediaFormat.KEY_BIT_RATE, AUDIO_BITRATE)
            setInteger(MediaFormat.KEY_AAC_PROFILE, MediaCodecInfo.CodecProfileLevel.AACObjectLC)
            setInteger(MediaFormat.KEY_MAX_INPUT_SIZE, minBuf * 4)
        }

        val enc = try {
            MediaCodec.createEncoderByType(MediaFormat.MIMETYPE_AUDIO_AAC).also { codec ->
                codec.configure(encFmt, null, null, MediaCodec.CONFIGURE_FLAG_ENCODE)
                codec.start()
            }
        } catch (e: Exception) {
            record.release()
            failGracefully("Audio encoder creation failed", e); return
        }

        audioRecord = record
        audioEncoder = enc
        actualAudioEnabled.set(true)
        record.startRecording()

        Thread({
            // Ensure the track latch is released even if the thread throws before emitting
            // INFO_OUTPUT_FORMAT_CHANGED, so the video thread is never blocked indefinitely.
            var audioLatchReleased = false
            runCatching {
                feedAndDrainAudio(record, enc, minBuf) { audioLatchReleased = true }
            }.onFailure {
                Log.e(TAG, "Audio encoder error", it)
                if (!audioLatchReleased) trackAddLatch.countDown()
            }
            onEncoderFinished()
        }, "ScreenRecorder-Audio").also { it.isDaemon = true; it.start() }
    }

    /**
     * Interleaved feed+drain loop for the audio encoder.
     * Reads PCM from [record], queues it into [enc], and drains [enc] output periodically.
     * [onLatchReleased] is called once INFO_OUTPUT_FORMAT_CHANGED has been handled (so the caller
     * knows the latch was released and need not release it again on error).
     */
    private fun feedAndDrainAudio(
        record: AudioRecord,
        enc: MediaCodec,
        bufSize: Int,
        onLatchReleased: () -> Unit,
    ) {
        val pcm = ByteArray(bufSize)
        var presentationUs = 0L
        val bytesPerFrame = AUDIO_CHANNELS * 2 // PCM 16-bit stereo
        val bufInfo = MediaCodec.BufferInfo()
        var eosQueued = false

        fun drainNonBlocking() {
            while (true) {
                val idx = enc.dequeueOutputBuffer(bufInfo, 0)
                when {
                    idx == MediaCodec.INFO_TRY_AGAIN_LATER -> return
                    idx == MediaCodec.INFO_OUTPUT_FORMAT_CHANGED -> {
                        handleFormatChanged(enc, isVideo = false)
                        onLatchReleased()
                    }
                    idx >= 0 -> {
                        writeSampleIfReady(enc, idx, bufInfo, isVideo = false)
                        if (bufInfo.flags and MediaCodec.BUFFER_FLAG_END_OF_STREAM != 0) return
                    }
                }
            }
        }

        while (!eosQueued) {
            if (stopRequested.get()) {
                val inIdx = enc.dequeueInputBuffer(DEQUEUE_TIMEOUT_US)
                if (inIdx >= 0) {
                    enc.queueInputBuffer(inIdx, 0, 0, presentationUs, MediaCodec.BUFFER_FLAG_END_OF_STREAM)
                    eosQueued = true
                }
            } else {
                val read = record.read(pcm, 0, pcm.size)
                when {
                    read > 0 -> {
                        val inIdx = enc.dequeueInputBuffer(DEQUEUE_TIMEOUT_US)
                        if (inIdx >= 0) {
                            val inBuf = enc.getInputBuffer(inIdx)!!
                            inBuf.clear()
                            inBuf.put(pcm, 0, read)
                            enc.queueInputBuffer(inIdx, 0, read, presentationUs, 0)
                            presentationUs += (read / bytesPerFrame).toLong() * 1_000_000L / AUDIO_SAMPLE_RATE
                        }
                    }
                    read < 0 -> {
                        // AudioRecord stopped or returned an error — queue EOS.
                        val inIdx = enc.dequeueInputBuffer(DEQUEUE_TIMEOUT_US)
                        if (inIdx >= 0) {
                            enc.queueInputBuffer(inIdx, 0, 0, presentationUs, MediaCodec.BUFFER_FLAG_END_OF_STREAM)
                            eosQueued = true
                        }
                    }
                    // read == 0: no data yet; loop and try again
                }
            }
            drainNonBlocking()
        }

        // Drain remaining output until EOS.
        drainEncoder(enc, isVideo = false)
    }

    // -------------------------------------------------------------------------
    // Common encoder drain (blocking until EOS)
    // -------------------------------------------------------------------------

    private fun drainEncoder(enc: MediaCodec, isVideo: Boolean) {
        val info = MediaCodec.BufferInfo()
        while (true) {
            val idx = enc.dequeueOutputBuffer(info, DEQUEUE_TIMEOUT_US)
            when {
                idx == MediaCodec.INFO_TRY_AGAIN_LATER -> { /* keep waiting */ }
                idx == MediaCodec.INFO_OUTPUT_FORMAT_CHANGED -> handleFormatChanged(enc, isVideo)
                idx >= 0 -> {
                    writeSampleIfReady(enc, idx, info, isVideo)
                    if (info.flags and MediaCodec.BUFFER_FLAG_END_OF_STREAM != 0) return
                }
            }
        }
    }

    /**
     * Called by an encoder thread when it observes INFO_OUTPUT_FORMAT_CHANGED.
     * Adds the track to the muxer, counts down the latch, and — once all tracks are
     * added — starts the muxer.
     */
    private fun handleFormatChanged(enc: MediaCodec, isVideo: Boolean) {
        val newFmt = enc.outputFormat
        synchronized(muxerLock) {
            if (isVideo) {
                videoTrackIndex = muxer.addTrack(newFmt)
            } else {
                audioTrackIndex = muxer.addTrack(newFmt)
            }
        }
        trackAddLatch.countDown()
        // Wait for the other encoder(s) to add their track, with a safety timeout so we never
        // deadlock if the other encoder fails before emitting INFO_OUTPUT_FORMAT_CHANGED.
        val allAdded = trackAddLatch.await(LATCH_TIMEOUT_MS, TimeUnit.MILLISECONDS)
        if (!allAdded) {
            Log.w(TAG, "Timed out waiting for all encoder tracks to be added; starting muxer with available tracks")
        }
        synchronized(muxerLock) {
            if (muxerStarted.compareAndSet(false, true)) {
                muxer.start()
                Log.d(TAG, "MediaMuxer started (videoTrack=$videoTrackIndex, audioTrack=$audioTrackIndex)")
            }
        }
    }

    private fun writeSampleIfReady(enc: MediaCodec, bufIndex: Int, info: MediaCodec.BufferInfo, isVideo: Boolean) {
        val trackIdx = if (isVideo) videoTrackIndex else audioTrackIndex
        if (trackIdx >= 0
            && muxerStarted.get()
            && info.size > 0
            && info.flags and MediaCodec.BUFFER_FLAG_CODEC_CONFIG == 0
        ) {
            val buf = enc.getOutputBuffer(bufIndex)
            if (buf != null) {
                buf.position(info.offset)
                buf.limit(info.offset + info.size)
                synchronized(muxerLock) {
                    muxer.writeSampleData(trackIdx, buf, info)
                }
            }
        }
        enc.releaseOutputBuffer(bufIndex, false)
    }

    // -------------------------------------------------------------------------
    // Finalization
    // -------------------------------------------------------------------------

    private fun onEncoderFinished() {
        if (encodersFinished.incrementAndGet() == totalEncoders) {
            finalizeMuxer()
        }
    }

    private fun finalizeMuxer() {
        if (!finalized.compareAndSet(false, true)) return
        try {
            synchronized(muxerLock) {
                if (muxerStarted.get()) muxer.stop()
                muxer.release()
            }
        } catch (e: Exception) {
            Log.e(TAG, "Error finalizing muxer", e)
        } finally {
            releaseEncoders()
        }
        onStopped(actualAudioEnabled.get())
    }

    private fun releaseEncoders() {
        runCatching { videoEncoder?.stop() }.onFailure { Log.w(TAG, "videoEncoder stop failed", it) }
        runCatching { videoEncoder?.release() }.onFailure { Log.w(TAG, "videoEncoder release failed", it) }
        runCatching { audioEncoder?.stop() }.onFailure { Log.w(TAG, "audioEncoder stop failed", it) }
        runCatching { audioEncoder?.release() }.onFailure { Log.w(TAG, "audioEncoder release failed", it) }
        runCatching { audioRecord?.release() }.onFailure { Log.w(TAG, "audioRecord release failed", it) }
        runCatching { virtualDisplay?.release() }.onFailure { Log.w(TAG, "virtualDisplay release failed", it) }
    }

    companion object {
        private const val TAG = "ScreenRecorder"
        private const val VIDEO_BITRATE = 4_000_000
        private const val VIDEO_FPS = 30
        private const val AUDIO_SAMPLE_RATE = 44_100
        private const val AUDIO_CHANNELS = 2
        private const val AUDIO_BITRATE = 128_000
        private const val AUDIO_BUFFER_BYTES = 8_192
        private const val DEQUEUE_TIMEOUT_US = 10_000L  // 10 ms
        private const val LATCH_TIMEOUT_MS = 5_000L     // 5 s
    }
}
