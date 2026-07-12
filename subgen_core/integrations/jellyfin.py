"""Jellyfin HTTP and JSON client algorithms."""

import json


def refresh_jellyfin_metadata(
    itemid: str,
    server_ip: str,
    jellyfin_token: str,
    *,
    timeout: float,
    request_client,
    logger,
) -> None:
    """Request a Jellyfin metadata refresh for a library item."""
    url = f"{server_ip}/Items/{itemid}/Refresh?MetadataRefreshMode=FullRefresh"
    headers = {"Authorization": f"MediaBrowser Token={jellyfin_token}"}
    response = request_client.post(url, headers=headers, timeout=timeout)

    if response.status_code == 204:
        logger.info("Metadata refresh queued successfully.")
        return
    raise Exception(f"Error refreshing metadata: {response.status_code}")


def get_jellyfin_file_name(
    item_id: str,
    jellyfin_url: str,
    jellyfin_token: str,
    *,
    timeout: float,
    request_client,
    logger,
) -> str:
    """Get the full media path for a Jellyfin library item."""
    headers = {"Authorization": f"MediaBrowser Token={jellyfin_token}"}
    users = json.loads(
        request_client.get(
            f"{jellyfin_url}/Users",
            headers=headers,
            timeout=timeout,
        ).content
    )
    jellyfin_admin = get_jellyfin_admin(users)

    response = request_client.get(
        f"{jellyfin_url}/Users/{jellyfin_admin}/Items/{item_id}",
        headers=headers,
        timeout=timeout,
    )
    if response.status_code == 200:
        return json.loads(response.content)["Path"]
    raise Exception(f"Error: {response.status_code}")


def get_jellyfin_admin(users):
    """Return the first Jellyfin administrator user ID."""
    for user in users:
        if user["Policy"]["IsAdministrator"]:
            return user["Id"]
    raise Exception("Unable to find administrator user in Jellyfin")
