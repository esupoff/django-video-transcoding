import os
import tempfile
from typing import Optional
from uuid import UUID, uuid4

import celery
import requests
from django.db.transaction import atomic

from video_transcoding import models, transcoding, defaults
from video_transcoding.celery import app
from video_transcoding.utils import LoggerMixin

DESTINATION_FILENAME = '{basename}1080p.mp4'


class TranscodeVideo(LoggerMixin, celery.Task):
    """ Video processing task."""
    name = 'video.transcode'
    routing_key = 'video_transcoding'

    def run(self, video_id: int) -> Optional[str]:
        """
        Process video.

        1. Locks video changing status from QUEUED to PROCESS
        2. Transcodes video and stores result to origins
        3. Changes video status to DONE, stores result basename
        4. On errors changes video status ERROR, stores error message

        :param video_id: Video id.
        """
        status = models.Video.DONE
        error = basename = None
        video = self.lock_video(video_id)
        try:
            basename = uuid4().hex
            self.process_video(video, basename)
        except Exception as e:
            basename = None
            status = models.Video.ERROR
            error = repr(e)
        finally:
            self.unlock_video(video_id, status, error, basename)
        return error

    def select_for_update(self, video_id: int, status: int) -> models.Video:
        """ Lock video in DB for current task.

        :param video_id: Video primary key
        :param status: expected video status
        :returns: Video object from db

        :raises models.Video.DoesNotExist: in case of missing or locked
        Video for primary key
        :raises ValueError: in case of unexpected Video status or task_id
        """
        try:
            video = models.Video.objects.select_for_update(
                skip_locked=True, of=('self',)).get(pk=video_id)
        except models.Video.DoesNotExist:
            self.logger.error("Can't lock video %s", video_id)
            raise

        if video.task_id != UUID(self.request.id):
            self.logger.error("Unexpected video %s task_id %s",
                              video.id, video.task_id)
            raise ValueError(video.task_id)

        if video.status != status:
            self.logger.error("Unexpected video %s status %s",
                              video.id, video.get_status_display())
            raise ValueError(video.status)
        return video

    @atomic
    def lock_video(self, video_id: int) -> models.Video:
        """
        Gets video in QUEUED status from DB and changes status to PROCESS.

        :param video_id: Video primary key
        :returns: Video object
        :raises Retry: in case of unexpected video status or task_id
        """
        try:
            video = self.select_for_update(video_id, models.Video.QUEUED)
        except (models.Video.DoesNotExist, ValueError) as e:
            # if video is locked or task_id is not equal to current task, retry.
            raise self.retry(exc=e)

        video.change_status(models.Video.PROCESS)
        return video

    @atomic
    def unlock_video(self, video_id: int, status: int, error: Optional[str],
                     basename: Optional[str]) -> None:
        """
        Marks video with final status.

        :param video_id: Video primary key
        :param status: final video status (Video.DONE, Video.ERROR)
        :param error: error message
        :param basename: UUID-like result file identifier
        :raises RuntimeError: in case of unexpected video status or task id
        """
        try:
            video = self.select_for_update(video_id, models.Video.PROCESS)
        except (models.Video.DoesNotExist, ValueError) as e:
            # if video is locked or task_id differs from current task, do
            # nothing because video is modified somewhere else.
            raise RuntimeError("Can't unlock locked video %s: %s",
                               video_id, repr(e))

        video.change_status(status, error=error, basename=basename)

    def process_video(self, video: models.Video, basename: str) -> None:
        """
        Video processing workflow.

        1. Create temporary directory
        2. Transcode source file
        3. Upload resulting file to origins
        4. Cleanup temporary directory
        """
        with tempfile.TemporaryDirectory(dir=defaults.VIDEO_TEMP_DIR,
                                         prefix=f'video-{video.pk}-') as d:
            destination = os.path.join(d, f'{basename}1080p.mp4')
            self.transcode(video.source, destination)
            self.store(destination)
        self.logger.info("Processing done")

    def transcode(self, source: str, destination: str) -> None:
        """
        Starts video transcoding

        :param source: source file link (http/ftp or file path)
        :param destination: result temporary file path.
        """
        self.logger.info("Start transcoding %s to %s",
                         source, destination)
        transcoder = transcoding.Transcoder(source, destination)
        transcoder.transcode()
        self.logger.info("Transcoding %s finished", source)

    def store(self, destination: str) -> None:
        """
        Stores transcoded video to origin list

        :param destination: transcoded video path.
        """
        self.logger.info("Start saving %s to origins", destination)
        filename = os.path.basename(destination)
        for origin in defaults.VIDEO_ORIGINS:
            url = os.path.join(origin, filename)
            self.logger.debug("Uploading %s to %s", destination, url)
            with open(destination, 'rb') as f:
                response = requests.put(url, data=f, timeout=(1, None))
                response.raise_for_status()
            self.logger.info("Uploaded to %s", url)
        self.logger.info("%s save finished", destination)


transcode_video = app.register_task(TranscodeVideo())  # type: ignore
