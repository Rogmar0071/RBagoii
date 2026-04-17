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
import okhttp3.Request
import okhttp3.RequestBody
import okhttp3.RequestBody.Companion.asRequestBody
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONObject
import java.io.File
import java.io.IOException
import java.util.UUID

/**
 * WorkManager worker that uploads a recorded clip to the backend.
 *
 * Upload strategy:
 *  - Files <= [CHUNK_THRESHOLD_BYTES] are uploaded in a single multipart POST to /v1/sessions.
 *  - Larger files are uploaded in [CHUNK_SIZE_BYTES]-sized chunks via POST /v1/sessions/chunks,
 *    followed by a PUT /v1/sessions/chunks/{uploadId}/finalize call to assemble them.
 *
 * Input data keys: [KEY_CLIP_PATH], [KEY_META_JSON]
 *
 * On success, output data contains: [KEY_SESSION_ID], [KEY_SESSION_STATUS]
 *
 * Uses [BackendClient] for a shared OkHttpClient with sane timeouts and automatic
 * retry/backoff to handle Render free-plan cold-start latency (502/timeout).
 * A stable [X_REQUEST_ID] header is sent with every attempt of the same work
 * item so the server can correlate retries in its logs.
 */
class UploadWorker(context: Context, params: WorkerParameters) : Worker(context, params) {

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
        val requestId = inputData.getString(KEY_REQUEST_ID) ?: UUID.randomUUID().toString()

        return if (clip.length() <= CHUNK_THRESHOLD_BYTES) {
            uploadSingle(clip, metaJson, baseUrl, apiKey, requestId)
        } else {
            uploadChunked(clip, metaJson, baseUrl, apiKey, requestId)
        }
    }

    // ---------------------------------------------------------------------------
    // Single-shot upload (small files)
    // ---------------------------------------------------------------------------

    private fun uploadSingle(
        clip: File,
        metaJson: String,
        baseUrl: String,
        apiKey: String,
        requestId: String,
    ): Result {
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
                .addHeader("X-Request-Id", requestId)
                .apply { if (apiKey.isNotEmpty()) addHeader("Authorization", "Bearer $apiKey") }
                .build()

            val response = BackendClient.executeWithRetry(request) { attempt, total ->
                Log.i(TAG, "Upload retry $attempt/$total (requestId=$requestId)")
            }

            response.use { resp ->
                val body = resp.body?.string() ?: ""
                if (!resp.isSuccessful) {
                    Log.e(TAG, "Upload failed: ${resp.code}")
                    return Result.failure(
                        Data.Builder().putString("error", "HTTP ${resp.code}: $body").build(),
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
            }
        } catch (e: IOException) {
            Log.e(TAG, "Upload network error after retries", e)
            Result.failure(Data.Builder().putString("error", e.message ?: "Network error").build())
        } catch (e: Exception) {
            Log.e(TAG, "Upload exception", e)
            Result.failure(Data.Builder().putString("error", e.message ?: "Unknown error").build())
        }
    }

    // ---------------------------------------------------------------------------
    // Chunked upload (large files)
    // ---------------------------------------------------------------------------

    private fun uploadChunked(
        clip: File,
        metaJson: String,
        baseUrl: String,
        apiKey: String,
        requestId: String,
    ): Result {
        val uploadId = UUID.randomUUID().toString()
        val fileSize = clip.length()
        val totalChunks = ((fileSize + CHUNK_SIZE_BYTES - 1) / CHUNK_SIZE_BYTES).toInt()
        Log.i(TAG, "Chunked upload: uploadId=$uploadId totalChunks=$totalChunks fileSize=$fileSize")

        return try {
            clip.inputStream().use { inputStream ->
                val buffer = ByteArray(CHUNK_SIZE_BYTES.toInt())
                var chunkIndex = 0
                var bytesRead: Int

                while (inputStream.read(buffer).also { bytesRead = it } != -1) {
                    val chunkData = buffer.copyOf(bytesRead)
                    val chunkBody = MultipartBody.Builder()
                        .setType(MultipartBody.FORM)
                        .addFormDataPart(
                            "chunk",
                            "chunk_$chunkIndex",
                            chunkData.toRequestBody("application/octet-stream".toMediaType()),
                        )
                        .build()

                    val chunkRequest = Request.Builder()
                        .url("$baseUrl/v1/sessions/chunks")
                        .post(chunkBody)
                        .addHeader("X-Upload-Id", uploadId)
                        .addHeader("X-Chunk-Index", chunkIndex.toString())
                        .addHeader("X-Total-Chunks", totalChunks.toString())
                        .addHeader("X-Request-Id", requestId)
                        .apply { if (apiKey.isNotEmpty()) addHeader("Authorization", "Bearer $apiKey") }
                        .build()

                    val chunkResp = BackendClient.executeWithRetry(chunkRequest)
                    chunkResp.use { resp ->
                        if (!resp.isSuccessful) {
                            throw IOException("Chunk $chunkIndex upload failed: HTTP ${resp.code}")
                        }
                    }
                    Log.i(TAG, "Chunk $chunkIndex/$totalChunks uploaded (uploadId=$uploadId)")
                    chunkIndex++
                }
            }

            // All chunks uploaded — finalize.
            val finalizeBody = MultipartBody.Builder()
                .setType(MultipartBody.FORM)
                .addFormDataPart("meta", metaJson)
                .build()

            val finalizeRequest = Request.Builder()
                .url("$baseUrl/v1/sessions/chunks/$uploadId/finalize")
                .put(finalizeBody)
                .addHeader("X-Request-Id", requestId)
                .apply { if (apiKey.isNotEmpty()) addHeader("Authorization", "Bearer $apiKey") }
                .build()

            val finalizeResp = BackendClient.executeWithRetry(finalizeRequest)
            finalizeResp.use { resp ->
                val body = resp.body?.string() ?: ""
                if (!resp.isSuccessful) {
                    return Result.failure(
                        Data.Builder().putString("error", "Finalize failed: HTTP ${resp.code}: $body").build(),
                    )
                }
                val json = JSONObject(body)
                val sessionId = json.optString("session_id", "")
                val status = json.optString("status", "unknown")
                Log.i(TAG, "Chunked upload finalized: session=$sessionId status=$status")
                Result.success(
                    Data.Builder()
                        .putString(KEY_SESSION_ID, sessionId)
                        .putString(KEY_SESSION_STATUS, status)
                        .build(),
                )
            }
        } catch (e: IOException) {
            Log.e(TAG, "Chunked upload network error", e)
            Result.failure(Data.Builder().putString("error", e.message ?: "Network error").build())
        } catch (e: Exception) {
            Log.e(TAG, "Chunked upload exception", e)
            Result.failure(Data.Builder().putString("error", e.message ?: "Unknown error").build())
        }
    }

    companion object {
        private const val TAG = "UploadWorker"

        const val KEY_CLIP_PATH = "clip_path"
        const val KEY_META_JSON = "meta_json"
        const val KEY_SESSION_ID = "session_id"
        const val KEY_SESSION_STATUS = "session_status"
        private const val KEY_REQUEST_ID = "request_id"

        /** Files larger than this threshold use chunked upload. */
        const val CHUNK_THRESHOLD_BYTES = 5L * 1024 * 1024  // 5 MB

        /** Size of each chunk for chunked uploads. */
        const val CHUNK_SIZE_BYTES = 1L * 1024 * 1024  // 1 MB

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
                        .putString(KEY_REQUEST_ID, UUID.randomUUID().toString())
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
