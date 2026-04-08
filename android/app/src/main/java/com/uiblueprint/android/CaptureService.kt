package com.uiblueprint.android

import android.app.Activity
import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.Service
import android.content.Intent
import android.media.projection.MediaProjection
import android.media.projection.MediaProjectionManager
import android.os.Handler
import android.os.IBinder
import android.os.Looper
import android.util.Log
import android.os.SystemClock
import androidx.core.app.NotificationCompat
import java.io.File
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

/**
 * Foreground service that captures the screen for [CLIP_DURATION_MS] ms using
 * MediaProjection + ScreenRecorder (MediaCodec + MediaMuxer) and broadcasts
 * [ACTION_CAPTURE_DONE] when finished.
 *
 * On API >= 29 with [EXTRA_AUDIO_ENABLED] = true, also captures internal playback audio
 * and muxes it into the same MP4.  Falls back to video-only silently if audio init fails.
 *
 * Start with an Intent containing:
 *   - [EXTRA_RESULT_CODE]    — Activity.RESULT_OK from MediaProjection permission
 *   - [EXTRA_RESULT_DATA]    — the Intent returned by the permission activity
 *   - [EXTRA_AUDIO_ENABLED]  — Boolean; true to request internal audio capture (optional)
 */
class CaptureService : Service() {

    private var mediaProjection: MediaProjection? = null
    private var screenRecorder: ScreenRecorder? = null
    private val handler = Handler(Looper.getMainLooper())
    private val finishRecordingRunnable = Runnable { finishRecording() }
    private lateinit var captureResultStore: CaptureResultStore
    private var isFinished = false
    private var recordingStartedAtMs: Long? = null
    private var audioEnabled = false

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onCreate() {
        super.onCreate()
        captureResultStore = SharedPreferencesCaptureResultStore(applicationContext)
        createNotificationChannel()
        startForeground(NOTIF_ID, buildNotification())
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        if (intent == null) {
            signalCaptureCompleted(CaptureDoneEvent(error = ERROR_CAPTURE_REQUEST_LOST))
            stopSelf()
            return START_NOT_STICKY
        }

        val resultCode = intent.getIntExtra(EXTRA_RESULT_CODE, -1)
        val resultData: Intent? = if (android.os.Build.VERSION.SDK_INT >= android.os.Build.VERSION_CODES.TIRAMISU) {
            intent.getParcelableExtra(EXTRA_RESULT_DATA, Intent::class.java)
        } else {
            @Suppress("DEPRECATION")
            intent.getParcelableExtra(EXTRA_RESULT_DATA)
        }
        audioEnabled = intent.getBooleanExtra(EXTRA_AUDIO_ENABLED, false)
        // Refresh the notification now that we know whether audio is enabled.
        getSystemService(NotificationManager::class.java)
            .notify(NOTIF_ID, buildNotification())

        if (resultCode != Activity.RESULT_OK) {
            signalCaptureCompleted(CaptureDoneEvent(error = ERROR_PERMISSION_UNAVAILABLE))
            stopSelf()
            return START_NOT_STICKY
        }

        if (resultData == null) {
            signalCaptureCompleted(CaptureDoneEvent(error = ERROR_PERMISSION_UNAVAILABLE))
            stopSelf()
            return START_NOT_STICKY
        }

        startRecording(resultCode, resultData)
        return START_NOT_STICKY
    }

    private fun startRecording(resultCode: Int, resultData: Intent) {
        val metrics = resources.displayMetrics
        val width = metrics.widthPixels
        val height = metrics.heightPixels
        val dpi = metrics.densityDpi

        val outputDir = getExternalFilesDir(null)
        if (outputDir == null) {
            signalCaptureCompleted(CaptureDoneEvent(error = ERROR_OUTPUT_UNAVAILABLE))
            stopSelf()
            return
        }

        val outputFile = File(
            outputDir,
            "clip_${SimpleDateFormat("yyyyMMdd_HHmmss", Locale.US).format(Date())}.mp4",
        )

        try {
            val mpm = getSystemService(MediaProjectionManager::class.java)
            val mp = mpm.getMediaProjection(resultCode, resultData)
            mp.registerCallback(object : MediaProjection.Callback() {
                override fun onStop() {
                    Log.d(TAG, "MediaProjection stopped externally")
                    finishRecording()
                }
            }, handler)
            mediaProjection = mp

            val recorder = ScreenRecorder(
                mediaProjection = mp,
                width = width,
                height = height,
                dpi = dpi,
                outputFile = outputFile,
                captureAudio = audioEnabled,
                onStopped = { audioWasEnabled -> onRecordingFinished(outputFile, audioWasEnabled) },
            )
            screenRecorder = recorder
            recorder.start()

            recordingStartedAtMs = SystemClock.elapsedRealtime()
            handler.postDelayed(finishRecordingRunnable, CLIP_DURATION_MS.toLong())
        } catch (e: Exception) {
            Log.e(TAG, "Failed to start recording", e)
            signalCaptureCompleted(CaptureDoneEvent(error = ERROR_START_FAILED))
            stopSelf()
        }
    }

    private fun finishRecording() {
        if (isFinished) return
        isFinished = true
        handler.removeCallbacks(finishRecordingRunnable)
        // stop() is idempotent; the onStopped callback on ScreenRecorder will call onRecordingFinished.
        screenRecorder?.stop()
    }

    /** Called on a background thread by [ScreenRecorder] once the MP4 is fully written. */
    private fun onRecordingFinished(outputFile: File, audioWasEnabled: Boolean) {
        Log.d(TAG, "Recording finished; audioEnabled=$audioWasEnabled file=${outputFile.absolutePath}")
        handler.post {
            val durationMs = recordingStartedAtMs
                ?.let { (SystemClock.elapsedRealtime() - it).toInt().coerceAtLeast(0) }
            mediaProjection?.stop()
            mediaProjection = null

            if (outputFile.exists() && outputFile.length() > 0) {
                signalCaptureCompleted(
                    CaptureDoneEvent(
                        clipPath = outputFile.absolutePath,
                        recordingDurationMs = durationMs,
                    ),
                )
            } else {
                signalCaptureCompleted(
                    CaptureDoneEvent(
                        error = ERROR_FINALIZE_FAILED,
                        recordingDurationMs = durationMs,
                    ),
                )
            }
            stopSelf()
        }
    }

    private fun signalCaptureCompleted(event: CaptureDoneEvent) {
        val normalizedEvent = event.normalized()
        captureResultStore.saveLastResult(normalizedEvent)
        sendBroadcast(Intent(ACTION_CAPTURE_DONE).apply {
            putExtra(EXTRA_SCHEMA_VERSION, normalizedEvent.schemaVersion)
            normalizedEvent.clipPath?.let { putExtra(EXTRA_CLIP_PATH, it) }
            normalizedEvent.error?.let { putExtra(EXTRA_ERROR, it) }
            normalizedEvent.recordingDurationMs?.let { putExtra(EXTRA_RECORDING_DURATION_MS, it) }
            setPackage(packageName)
        })
    }

    override fun onDestroy() {
        handler.removeCallbacks(finishRecordingRunnable)
        screenRecorder?.stop()
        mediaProjection?.stop()
        super.onDestroy()
    }

    // -------------------------------------------------------------------------
    // Notification
    // -------------------------------------------------------------------------

    private fun createNotificationChannel() {
        val nm = getSystemService(NotificationManager::class.java)
        nm.createNotificationChannel(
            NotificationChannel(CHANNEL_ID, "Screen Recording", NotificationManager.IMPORTANCE_LOW),
        )
    }

    private fun buildNotification(): Notification {
        val contentText = if (audioEnabled) {
            getString(R.string.notif_recording_with_audio)
        } else {
            getString(R.string.notif_recording_video_only)
        }
        return NotificationCompat.Builder(this, CHANNEL_ID)
            .setContentTitle(getString(R.string.notif_recording_title))
            .setContentText(contentText)
            .setSmallIcon(android.R.drawable.ic_media_play)
            .setOngoing(true)
            .build()
    }

    companion object {
        private const val TAG = "CaptureService"

        const val ACTION_CAPTURE_DONE = "com.uiblueprint.android.CAPTURE_DONE"
        const val EXTRA_RESULT_CODE = "result_code"
        const val EXTRA_RESULT_DATA = "result_data"
        const val EXTRA_AUDIO_ENABLED = "audio_enabled"
        const val EXTRA_CLIP_PATH = "clip_path"
        const val EXTRA_ERROR = "error"
        const val EXTRA_SCHEMA_VERSION = "schema_version"
        const val EXTRA_RECORDING_DURATION_MS = "recording_duration_ms"

        private const val CHANNEL_ID = "capture_channel"
        private const val NOTIF_ID = 1001
        private const val CLIP_DURATION_MS = 20_000
        private const val ERROR_CAPTURE_REQUEST_LOST = "Screen capture could not be started."
        private const val ERROR_PERMISSION_UNAVAILABLE = "Screen capture permission data was unavailable."
        private const val ERROR_OUTPUT_UNAVAILABLE = "Capture output could not be created."
        private const val ERROR_START_FAILED = "Capture failed to start recording."
        private const val ERROR_FINALIZE_FAILED = "Capture failed to finalize recording."
    }
}
