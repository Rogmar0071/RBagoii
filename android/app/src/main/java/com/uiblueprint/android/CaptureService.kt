package com.uiblueprint.android

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.Service
import android.content.Intent
import android.hardware.display.DisplayManager
import android.hardware.display.VirtualDisplay
import android.media.MediaRecorder
import android.media.projection.MediaProjection
import android.media.projection.MediaProjectionManager
import android.os.Handler
import android.os.IBinder
import android.os.Looper
import android.util.DisplayMetrics
import android.util.Log
import androidx.core.app.NotificationCompat
import java.io.File
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

/**
 * Foreground service that captures the screen for [CLIP_DURATION_MS] ms using
 * MediaProjection + MediaRecorder and broadcasts [ACTION_CAPTURE_DONE] when
 * finished.
 *
 * Start with an Intent containing:
 *   - [EXTRA_RESULT_CODE]  — Activity.RESULT_OK from MediaProjection permission
 *   - [EXTRA_RESULT_DATA]  — the Intent returned by the permission activity
 */
class CaptureService : Service() {

    private var mediaProjection: MediaProjection? = null
    private var virtualDisplay: VirtualDisplay? = null
    private var mediaRecorder: MediaRecorder? = null
    private var outputFile: File? = null
    private val handler = Handler(Looper.getMainLooper())
    private var isFinished = false

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onCreate() {
        super.onCreate()
        createNotificationChannel()
        startForeground(NOTIF_ID, buildNotification())
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        if (intent == null) {
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

        if (resultData == null) {
            broadcastError("Missing MediaProjection result data")
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

        outputFile = File(
            getExternalFilesDir(null),
            "clip_${SimpleDateFormat("yyyyMMdd_HHmmss", Locale.US).format(Date())}.mp4",
        )

        try {
            mediaRecorder = MediaRecorder(this).apply {
                setVideoSource(MediaRecorder.VideoSource.SURFACE)
                setOutputFormat(MediaRecorder.OutputFormat.MPEG_4)
                setVideoEncoder(MediaRecorder.VideoEncoder.H264)
                setVideoEncodingBitRate(VIDEO_BITRATE)
                setVideoFrameRate(VIDEO_FPS)
                setVideoSize(width, height)
                setOutputFile(outputFile!!.absolutePath)
                prepare()
            }

            val mpm = getSystemService(MediaProjectionManager::class.java)
            mediaProjection = mpm.getMediaProjection(resultCode, resultData).also { mp ->
                mp.registerCallback(object : MediaProjection.Callback() {
                    override fun onStop() {
                        Log.d(TAG, "MediaProjection stopped externally")
                        finishRecording()
                    }
                }, handler)

                virtualDisplay = mp.createVirtualDisplay(
                    "UIBlueprintCapture",
                    width, height, dpi,
                    DisplayManager.VIRTUAL_DISPLAY_FLAG_AUTO_MIRROR,
                    mediaRecorder!!.surface, null, handler,
                )
            }

            mediaRecorder!!.start()

            // Stop after CLIP_DURATION_MS.
            handler.postDelayed({ finishRecording() }, CLIP_DURATION_MS.toLong())
        } catch (e: Exception) {
            Log.e(TAG, "Failed to start recording", e)
            broadcastError(e.message ?: "Unknown error")
            stopSelf()
        }
    }

    private fun finishRecording() {
        if (isFinished) return
        isFinished = true
        try {
            mediaRecorder?.stop()
        } catch (_: Exception) {
        }
        mediaRecorder?.release()
        mediaRecorder = null
        virtualDisplay?.release()
        virtualDisplay = null
        mediaProjection?.stop()
        mediaProjection = null

        val clip = outputFile
        if (clip != null && clip.exists() && clip.length() > 0) {
            broadcastDone(clip)
        } else {
            broadcastError("Output file missing or empty")
        }
        stopSelf()
    }

    private fun broadcastDone(clip: File) {
        sendBroadcast(Intent(ACTION_CAPTURE_DONE).apply {
            putExtra(EXTRA_CLIP_PATH, clip.absolutePath)
            setPackage(packageName)
        })
    }

    private fun broadcastError(message: String) {
        sendBroadcast(Intent(ACTION_CAPTURE_DONE).apply {
            putExtra(EXTRA_ERROR, message)
            setPackage(packageName)
        })
    }

    override fun onDestroy() {
        handler.removeCallbacksAndMessages(null)
        mediaRecorder?.release()
        virtualDisplay?.release()
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

    private fun buildNotification(): Notification =
        NotificationCompat.Builder(this, CHANNEL_ID)
            .setContentTitle("Recording screen…")
            .setContentText("Recording a 10-second clip")
            .setSmallIcon(android.R.drawable.ic_media_play)
            .setOngoing(true)
            .build()

    companion object {
        private const val TAG = "CaptureService"

        const val ACTION_CAPTURE_DONE = "com.uiblueprint.android.CAPTURE_DONE"
        const val EXTRA_RESULT_CODE = "result_code"
        const val EXTRA_RESULT_DATA = "result_data"
        const val EXTRA_CLIP_PATH = "clip_path"
        const val EXTRA_ERROR = "error"

        private const val CHANNEL_ID = "capture_channel"
        private const val NOTIF_ID = 1001
        private const val CLIP_DURATION_MS = 10_000
        private const val VIDEO_BITRATE = 4_000_000
        private const val VIDEO_FPS = 30
    }
}
