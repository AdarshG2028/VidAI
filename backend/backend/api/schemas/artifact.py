"""Response schemas for artifact listing (Phase 5A).

A job's output is a set of typed assets per stage, not one file: a chain
ending in render leaves an edited video, and a chain including transcribe
also leaves a transcript and a subtitle file the user may well want on
its own. Listing them per stage is also what lets a client show *which*
step produced a bad result rather than only the final one.
"""

import uuid
from urllib.parse import quote

from pydantic import BaseModel


class ArtifactResponse(BaseModel):
    kind: str
    uri: str
    download_url: str

    @classmethod
    def from_asset(cls, kind: str, uri: str, job_id: uuid.UUID) -> "ArtifactResponse":
        # The URI is passed back whole as a query parameter rather than
        # being split into a path segment: backend/storage/base.py's
        # contract is that callers store a URI and hand it back untouched,
        # never parse or construct one. Keeping that true here means an
        # S3-backed Storage needs no change to this route.
        #
        # job_id rides along because the download is authorized against
        # the room that job belongs to (Phase 8). The route stays flat
        # rather than nested under /jobs/{id}: the URI is still the
        # address, the job is only the authorization context -- which
        # matters because forwarded assets mean one object legitimately
        # belongs to several jobs.
        return cls(
            kind=kind,
            uri=uri,
            download_url=f"/artifacts?uri={quote(uri, safe='')}&job_id={job_id}",
        )


class StageArtifactsResponse(BaseModel):
    stage: int
    worker_name: str
    artifacts: list[ArtifactResponse]


class JobArtifactsResponse(BaseModel):
    job_id: uuid.UUID
    status: str
    stages: list[StageArtifactsResponse]
