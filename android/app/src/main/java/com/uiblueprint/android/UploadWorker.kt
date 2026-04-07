package com.uiblueprint.android

import android.content.Context
import android.util.Log
import androidx.work.Data
import androidx.work.OneTimeWorkRequestBuilder
import androidx.work.WorkInfo
import androidx.work.WorkManager
import androidx.work.Worker
import androidx.work.WorkerParameters
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.MultipartBody
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.asRequestBody
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONObject
import java.io.File
import java.util.UUID
import java.util.concurrent.TimeUnit

/**
 * WorkManager worker that uploads a recorded clip to the backend.
 *
 * Input data keys: [KEY_CLIP_PATH], [KEY_META_JSON]
 *
 * On success, output data contains: [KEY_SESSION_ID], [KEY_SESSION_STATUS]
 */
class UploadWorker(context: Context, params: WorkerParameters) : Worker(context, params) {

    private val client = OkHttpClient.Builder()
        .connectTimeout(30, TimeUnit.SECONDS)
        .writeTimeout(120, TimeUnit.SECONDS)
        .readTimeout(60, TimeUnit.SECONDS)
        .build()

    override fun doWork(): Result {
        val clipPath = inputData.getString(KEY_CLIP_PATH)
            ?: return Result.failure(Data.Builder().putString("error", "Missing clip path").build())
        val metaJson = inputData.getString(KEY_META_JSON) ?: "{}"

        val clip = File(clipPath)
        if (!clip.exists()) {
            return Result.failure(Data.Builder().putString("error", "Clip file not found: $clipPath").build())
        }

        val baseUrl = BuildConfig.BACKEND_BASE_URL.trimEnd('/')
        val apiKey = BuildConfig.BACKEND_API_KEY

        return try {
            val requestBody = MultipartBody.Builder()
                .setType(MultipartBody.FORM)
                .addFormDataPart(
                    "video",
                    clip.name,
                    clip.asRequestBody("video/mp4".toMediaType()),
                )
                .addFormDataPart("meta", metaJson)
                .build()

            val request = Request.Builder()
                .url("$baseUrl/v1/sessions")
                .post(requestBody)
                .apply { if (apiKey.isNotEmpty()) addHeader("Authorization", "Bearer $apiKey") }
                .build()

            val response = client.newCall(request).execute()
            val body = response.body?.string() ?: ""

            if (!response.isSuccessful) {
                Log.e(TAG, "Upload failed: ${response.code} $body")
                return Result.failure(
                    Data.Builder()
                        .putString("error", "HTTP ${response.code}: $body")
                        .build(),
                )
            }

            val json = JSONObject(body)
            val sessionId = json.optString("session_id", "")
            val status = json.optString("status", "unknown")

            Log.i(TAG, "Upload succeeded: session=$sessionId status=$status")
            Result.success(
                Data.Builder()
                    .putString(KEY_SESSION_ID, sessionId)
                    .putString(KEY_SESSION_STATUS, status)
                    .build(),
            )
        } catch (e: Exception) {
            Log.e(TAG, "Upload exception", e)
            Result.failure(
                Data.Builder()
                    .putString("error", e.message ?: "Network error")
                    .build(),
            )
        }
    }

    companion object {
        private const val TAG = "UploadWorker"

        const val KEY_CLIP_PATH = "clip_path"
        const val KEY_META_JSON = "meta_json"
        const val KEY_SESSION_ID = "session_id"
        const val KEY_SESSION_STATUS = "session_status"

        /**
         * Enqueue an upload task for [clipPath] and return a unique tag that
         * can be used to query the work state later.
         */
        fun enqueue(context: Context, clipPath: String, metaJson: String): String {
            val tag = "upload_${UUID.randomUUID()}"
            val request = OneTimeWorkRequestBuilder<UploadWorker>()
                .setInputData(
                    Data.Builder()
                        .putString(KEY_CLIP_PATH, clipPath)
                        .putString(KEY_META_JSON, metaJson)
                        .build(),
                )
                .addTag(tag)
                .build()

            WorkManager.getInstance(context).enqueue(request)
            return tag
        }

        /**
         * Return a human-readable state string for the work tagged [tag].
         */
        fun getState(context: Context, tag: String): String {
            val infos = WorkManager.getInstance(context)
                .getWorkInfosByTag(tag)
                .get() ?: return "unknown"
            val info = infos.firstOrNull() ?: return "unknown"
            return when (info.state) {
                WorkInfo.State.ENQUEUED -> "enqueued"
                WorkInfo.State.RUNNING -> "running"
                WorkInfo.State.SUCCEEDED -> "succeeded"
                WorkInfo.State.FAILED -> "failed"
                WorkInfo.State.BLOCKED -> "blocked"
                WorkInfo.State.CANCELLED -> "cancelled"
            }
        }
    }
}
