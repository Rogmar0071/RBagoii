package com.uiblueprint.android

import android.content.Context
import android.graphics.drawable.Drawable
import androidx.core.content.ContextCompat
import java.util.Date

/**
 * The three user-visible states for any ingested item (file or repository).
 *
 * Maps backend IngestJob states:
 *   UPLOADING  ← created | stored | queued
 *   ANALYZING  ← running | processing | indexing | finalizing
 *   AVAILABLE  ← success
 *   FAILED     ← failed
 */
enum class IngestStatus {
    UPLOADING,
    ANALYZING,
    AVAILABLE,
    FAILED;

    companion object {
        /** Derive display status from a raw backend IngestJob status string. */
        fun fromBackendStatus(status: String): IngestStatus = when (status) {
            "created", "stored", "queued" -> UPLOADING
            "running", "processing", "indexing", "finalizing" -> ANALYZING
            "success" -> AVAILABLE
            "failed" -> FAILED
            else -> UPLOADING
        }
    }
}

/**
 * Bind a status pill TextView to the given [IngestStatus].
 *
 * Sets visibility, background drawable, text colour, leading icon, and label.
 * Hides the view when status is null.
 */
fun android.widget.TextView.bindIngestStatus(status: IngestStatus?) {
    if (status == null) {
        visibility = android.view.View.GONE
        return
    }
    visibility = android.view.View.VISIBLE

    val ctx = context
    when (status) {
        IngestStatus.UPLOADING -> {
            setBackgroundResource(R.drawable.bg_status_pill_uploading)
            setTextColor(ContextCompat.getColor(ctx, android.R.color.white))
            setText(R.string.status_ingest_uploading)
            setCompoundDrawablesWithIntrinsicBounds(
                ContextCompat.getDrawable(ctx, R.drawable.ic_status_uploading), null, null, null
            )
        }
        IngestStatus.ANALYZING -> {
            setBackgroundResource(R.drawable.bg_status_pill_analyzing)
            setTextColor(ContextCompat.getColor(ctx, R.color.status_pill_text_dark))
            setText(R.string.status_ingest_analyzing)
            setCompoundDrawablesWithIntrinsicBounds(
                ContextCompat.getDrawable(ctx, R.drawable.ic_status_analyzing), null, null, null
            )
        }
        IngestStatus.AVAILABLE -> {
            setBackgroundResource(R.drawable.bg_status_pill_available)
            setTextColor(ContextCompat.getColor(ctx, R.color.status_pill_text_muted))
            setText(R.string.status_ingest_available)
            setCompoundDrawablesWithIntrinsicBounds(
                ContextCompat.getDrawable(ctx, R.drawable.ic_status_available), null, null, null
            )
        }
        IngestStatus.FAILED -> {
            setBackgroundResource(R.drawable.bg_status_pill_uploading)
            setTextColor(ContextCompat.getColor(ctx, android.R.color.white))
            setText(R.string.status_ingest_failed)
            setCompoundDrawablesWithIntrinsicBounds(null, null, null, null)
        }
    }
}

/**
 * Data model for a file uploaded to a chat conversation.
 * Maps to the ChatFile backend model.
 */
data class ChatFile(
    val id: String,
    val conversationId: String,
    var filename: String,
    val mimeType: String,
    val sizeBytes: Long,
    val category: String,
    var includedInContext: Boolean,
    val createdAt: Date,
    val updatedAt: Date,
    val downloadUrl: String?,
    var ingestStatus: IngestStatus = IngestStatus.AVAILABLE,
)

/**
 * File category enum for grouping files in the UI.
 */
enum class FileCategory(val value: String) {
    DOCUMENT("document"),
    CODE("code"),
    IMAGE("image"),
    VIDEO("video"),
    AUDIO("audio"),
    DATA("data"),
    ARCHIVE("archive"),
    OTHER("other");

    companion object {
        fun fromString(value: String): FileCategory {
            return values().find { it.value == value } ?: OTHER
        }
    }
}

/**
 * Helper to format file size in human-readable format.
 */
fun formatFileSize(bytes: Long): String {
    val kb = bytes / 1024.0
    val mb = kb / 1024.0
    val gb = mb / 1024.0

    return when {
        gb >= 1.0 -> String.format("%.1f GB", gb)
        mb >= 1.0 -> String.format("%.1f MB", mb)
        kb >= 1.0 -> String.format("%.1f KB", kb)
        else -> "$bytes B"
    }
}
