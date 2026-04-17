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
import java.io.File
import java.io.FileInputStream
import java.util.UUID

/**
 * Helper class for uploading files in chunks.
 * 
 * For files larger than CHUNK_SIZE_BYTES (5 MB), the file is split into chunks
 * and uploaded using the chunked upload API endpoints.
 * 
 * Images are always uploaded as single files (no chunking).
 */
object ChatFileUploadHelper {

    private const val CHUNK_SIZE_BYTES = 5 * 1024 * 1024 // 5 MB
    private const val IMAGE_MIME_PREFIX = "image/"

    /**
     * Upload a file to the conversation, using chunked upload if necessary.
     * 
     * @param uri The URI of the file to upload
     * @param conversationId The conversation ID
     * @param apiKey The API key for authorization
     * @param baseUrl The backend base URL
     * @param contentResolver The content resolver for file access
     * @param cacheDir The cache directory for temporary files
     * @param onProgress Callback for upload progress (chunk index, total chunks)
     * @return true if successful, false otherwise
     */
    fun uploadFile(
        uri: Uri,
        conversationId: String,
        apiKey: String,
        baseUrl: String,
        contentResolver: ContentResolver,
        cacheDir: File,
        onProgress: ((Int, Int) -> Unit)? = null
    ): Boolean {
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
            val fileSize = tempFile.length()

            // Use chunked upload for non-image files larger than CHUNK_SIZE_BYTES
            val useChunkedUpload = !mimeType.startsWith(IMAGE_MIME_PREFIX) && fileSize > CHUNK_SIZE_BYTES

            val success = if (useChunkedUpload) {
                uploadChunked(
                    tempFile,
                    filename,
                    mimeType,
                    conversationId,
                    apiKey,
                    baseUrl,
                    onProgress
                )
            } else {
                uploadSingle(
                    tempFile,
                    filename,
                    mimeType,
                    conversationId,
                    apiKey,
                    baseUrl
                )
            }

            tempFile.delete()
            success
        } catch (e: Exception) {
            Log.e("ChatFileUpload", "Error uploading file", e)
            false
        }
    }

    private fun uploadSingle(
        file: File,
        filename: String,
        mimeType: String,
        conversationId: String,
        apiKey: String,
        baseUrl: String
    ): Boolean {
        return try {
            val requestBody = MultipartBody.Builder()
                .setType(MultipartBody.FORM)
                .addFormDataPart(
                    "file",
                    filename,
                    file.asRequestBody(mimeType.toMediaType())
                )
                .build()

            val request = Request.Builder()
                .url("$baseUrl/api/chat/$conversationId/files")
                .addHeader("Authorization", "Bearer $apiKey")
                .post(requestBody)
                .build()

            val response = BackendClient.executeWithRetry(request)
            response.isSuccessful
        } catch (e: Exception) {
            Log.e("ChatFileUpload", "Single upload failed", e)
            false
        }
    }

    private fun uploadChunked(
        file: File,
        filename: String,
        mimeType: String,
        conversationId: String,
        apiKey: String,
        baseUrl: String,
        onProgress: ((Int, Int) -> Unit)? = null
    ): Boolean {
        return try {
            val uploadId = UUID.randomUUID().toString()
            val fileSize = file.length()
            val totalChunks = ((fileSize + CHUNK_SIZE_BYTES - 1) / CHUNK_SIZE_BYTES).toInt()

            // Upload each chunk
            FileInputStream(file).use { input ->
                val buffer = ByteArray(CHUNK_SIZE_BYTES)
                var chunkIndex = 0

                while (true) {
                    val bytesRead = input.read(buffer)
                    if (bytesRead <= 0) break

                    val chunkData = if (bytesRead < buffer.size) {
                        buffer.copyOf(bytesRead)
                    } else {
                        buffer
                    }

                    // Upload chunk
                    val chunkFile = File(file.parent, "${file.name}.chunk_$chunkIndex")
                    chunkFile.writeBytes(chunkData)

                    val requestBody = MultipartBody.Builder()
                        .setType(MultipartBody.FORM)
                        .addFormDataPart(
                            "chunk",
                            "chunk",
                            chunkFile.asRequestBody("application/octet-stream".toMediaType())
                        )
                        .build()

                    val request = Request.Builder()
                        .url("$baseUrl/api/chat/$conversationId/files/chunks")
                        .addHeader("Authorization", "Bearer $apiKey")
                        .addHeader("X-Upload-Id", uploadId)
                        .addHeader("X-Chunk-Index", chunkIndex.toString())
                        .addHeader("X-Total-Chunks", totalChunks.toString())
                        .addHeader("X-Filename", filename)
                        .post(requestBody)
                        .build()

                    val response = BackendClient.executeWithRetry(request)
                    chunkFile.delete()

                    if (!response.isSuccessful) {
                        Log.e("ChatFileUpload", "Chunk $chunkIndex upload failed: ${response.code}")
                        return false
                    }

                    onProgress?.invoke(chunkIndex + 1, totalChunks)
                    chunkIndex++
                }
            }

            // Finalize upload
            val finalizeBody = MultipartBody.Builder()
                .setType(MultipartBody.FORM)
                .addFormDataPart("filename", filename)
                .addFormDataPart("mime_type", mimeType)
                .build()

            val finalizeRequest = Request.Builder()
                .url("$baseUrl/api/chat/$conversationId/files/chunks/$uploadId/finalize")
                .addHeader("Authorization", "Bearer $apiKey")
                .put(finalizeBody)
                .build()

            val finalizeResponse = BackendClient.executeWithRetry(finalizeRequest)
            finalizeResponse.isSuccessful
        } catch (e: Exception) {
            Log.e("ChatFileUpload", "Chunked upload failed", e)
            false
        }
    }
}
