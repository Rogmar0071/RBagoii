package com.uiblueprint.android

import android.content.ContentResolver
import android.net.Uri
import android.provider.OpenableColumns
import android.util.Log
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.MultipartBody
import okhttp3.Request
import okhttp3.RequestBody.Companion.asRequestBody
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONObject
import java.io.File
import java.io.FileInputStream
import java.util.UUID

/**
 * Helper class for uploading files via the unified ingestion API.
 * 
 * MQP-CONTRACT: INGESTION_UI_STATE_ALIGNMENT_V1
 * Uses POST /v1/ingest/file endpoint which returns job_id for polling.
 */
object ChatFileUploadHelper {

    private const val CHUNK_SIZE_BYTES = 5 * 1024 * 1024 // 5 MB
    private const val IMAGE_MIME_PREFIX = "image/"

    /**
     * Upload a file via the unified ingestion endpoint.
     * 
     * @param uri The URI of the file to upload
     * @param conversationId The conversation ID
     * @param apiKey The API key for authorization
     * @param baseUrl The backend base URL
     * @param contentResolver The content resolver for file access
     * @param cacheDir The cache directory for temporary files
     * @param onProgress Callback for upload progress (deprecated - now handled by polling)
     * @return job_id if successful, null otherwise
     */
    fun uploadFile(
        uri: Uri,
        conversationId: String,
        apiKey: String,
        baseUrl: String,
        contentResolver: ContentResolver,
        cacheDir: File,
        onProgress: ((Int, Int) -> Unit)? = null
    ): String? {
        return try {
            // Get file info
            val cursor = contentResolver.query(uri, null, null, null, null)
            val filename = cursor?.use {
                if (it.moveToFirst()) {
                    val nameIndex = it.getColumnIndex(OpenableColumns.DISPLAY_NAME)
                    if (nameIndex >= 0) it.getString(nameIndex) else "file"
                } else "file"
            } ?: "file"

            // Copy file to temp location
            val tempFile = File(cacheDir, filename)
            contentResolver.openInputStream(uri)?.use { input ->
                tempFile.outputStream().use { output ->
                    input.copyTo(output)
                }
            }

            val mimeType = contentResolver.getType(uri) ?: "application/octet-stream"

            // Use the unified ingestion endpoint (no chunking)
            val jobId = uploadViaIngestEndpoint(
                tempFile,
                filename,
                conversationId,
                apiKey,
                baseUrl
            )

            tempFile.delete()
            jobId
        } catch (e: Exception) {
            Log.e("ChatFileUpload", "Error uploading file", e)
            null
        }
    }

    /**
     * Upload file via unified ingestion endpoint POST /v1/ingest/file.
     * Returns job_id which must be polled for status.
     * 
     * SOURCE OF TRUTH: POST /v1/ingest/file
     * REQUIRED: Store job_id for polling
     */
    private fun uploadViaIngestEndpoint(
        file: File,
        filename: String,
        conversationId: String,
        apiKey: String,
        baseUrl: String
    ): String? {
        return try {
            val requestBody = MultipartBody.Builder()
                .setType(MultipartBody.FORM)
                .addFormDataPart(
                    "file",
                    filename,
                    file.asRequestBody("application/octet-stream".toMediaType())
                )
                .addFormDataPart("conversation_id", conversationId)
                .build()

            val request = Request.Builder()
                .url("$baseUrl/v1/ingest/file")
                .addHeader("Authorization", "Bearer $apiKey")
                .post(requestBody)
                .build()

            val response = BackendClient.executeWithRetry(request)
            
            if (response.isSuccessful) {
                val responseBody = response.body?.string() ?: "{}"
                val responseJson = JSONObject(responseBody)
                val jobId = responseJson.getString("job_id")
                Log.d("ChatFileUpload", "File upload initiated with job_id=$jobId")
                jobId
            } else {
                Log.e("ChatFileUpload", "Upload failed: ${response.code}")
                null
            }
        } catch (e: Exception) {
            Log.e("ChatFileUpload", "Upload via ingest endpoint failed", e)
            null
        }
    }
}
