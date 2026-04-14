package com.uiblueprint.android

import android.util.Log
import okhttp3.Request
import org.json.JSONObject
import java.io.IOException
import java.util.concurrent.Executors
import java.util.concurrent.Future
import java.util.concurrent.ScheduledExecutorService
import java.util.concurrent.ScheduledFuture
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicBoolean

/**
 * Polls ``GET /v1/analysis/{jobId}`` on a background thread at a fixed interval
 * until the job reaches a terminal state (succeeded / failed) or [stop] is called.
 *
 * Usage
 * -----
 * ```kotlin
 * val poller = AnalysisStatusPoller(baseUrl, apiKey, jobId) { status, result ->
 *     runOnUiThread { updateUi(status, result) }
 * }
 * poller.start()
 * // …later…
 * poller.stop()
 * ```
 *
 * [onUpdate] is called on the polling thread; callers that update Android Views
 * must post to the main thread themselves (e.g. ``runOnUiThread { … }``).
 */
class AnalysisStatusPoller(
    private val baseUrl: String,
    private val apiKey: String,
    private val jobId: String,
    /** Called each time a status response arrives. */
    private val onUpdate: (status: String, payload: JSONObject) -> Unit,
    /** Called once when the job reaches a terminal state or an unrecoverable error occurs. */
    private val onTerminal: ((status: String, payload: JSONObject) -> Unit)? = null,
    /** Polling interval in seconds. */
    private val intervalSeconds: Long = 3L,
) {
    private val scheduler: ScheduledExecutorService =
        Executors.newSingleThreadScheduledExecutor { Thread(it, "AnalysisPoller-$jobId") }

    private val stopped = AtomicBoolean(false)
    private var future: ScheduledFuture<*>? = null

    /** Start polling. Safe to call multiple times — only the first call has effect. */
    fun start() {
        if (stopped.get()) return
        future = scheduler.scheduleWithFixedDelay(
            ::poll,
            0L,
            intervalSeconds,
            TimeUnit.SECONDS,
        )
    }

    /** Stop polling. Idempotent. */
    fun stop() {
        stopped.set(true)
        future?.cancel(false)
        scheduler.shutdownNow()
    }

    private fun poll() {
        if (stopped.get()) return
        try {
            val url = "${baseUrl.trimEnd('/')}/v1/analysis/$jobId"
            val request = Request.Builder()
                .url(url)
                .get()
                .apply { if (apiKey.isNotEmpty()) addHeader("Authorization", "Bearer $apiKey") }
                .build()

            val response = BackendClient.httpClient.newCall(request).execute()
            response.use { resp ->
                val bodyStr = resp.body?.string() ?: ""
                if (!resp.isSuccessful) {
                    Log.w(TAG, "Poll $jobId: HTTP ${resp.code}")
                    return
                }
                val json = runCatching { JSONObject(bodyStr) }.getOrNull() ?: return
                val status = json.optString("status", "unknown")
                onUpdate(status, json)

                if (status in TERMINAL_STATUSES) {
                    Log.i(TAG, "Job $jobId reached terminal status: $status")
                    onTerminal?.invoke(status, json)
                    stop()
                }
            }
        } catch (e: IOException) {
            Log.w(TAG, "Poll $jobId network error: ${e.message}")
        } catch (e: Exception) {
            Log.e(TAG, "Poll $jobId unexpected error", e)
        }
    }

    companion object {
        private const val TAG = "AnalysisStatusPoller"
        val TERMINAL_STATUSES = setOf("succeeded", "failed")
    }
}
