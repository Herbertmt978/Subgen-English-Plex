"""Plex HTTP and XML client algorithms."""

import xml.etree.ElementTree as ET


def get_next_plex_episode(
    current_episode_rating_key,
    server_ip: str,
    plex_token: str,
    *,
    stay_in_season: bool = False,
    timeout: float,
    request_client,
    logger,
):
    """Return the next Plex episode rating key, or ``None`` at the boundary."""
    try:
        url = f"{server_ip}/library/metadata/{current_episode_rating_key}"
        headers = {"X-Plex-Token": plex_token}
        response = request_client.get(url, headers=headers, timeout=timeout)
        response.raise_for_status()

        root = ET.fromstring(response.content)
        grandparent_rating_key = root.find(".//Video").get("grandparentRatingKey")
        if grandparent_rating_key is None:
            logger.debug(f"Show not found for episode {current_episode_rating_key}")
            return None

        parent_rating_key = root.find(".//Video").get("parentRatingKey")
        if parent_rating_key is None:
            logger.debug(f"Parent season not found for episode {current_episode_rating_key}")
            return None

        url = f"{server_ip}/library/metadata/{grandparent_rating_key}/children"
        response = request_client.get(url, headers=headers, timeout=timeout)
        response.raise_for_status()
        seasons = ET.fromstring(response.content).findall(".//Directory[@type='season']")

        url = f"{server_ip}/library/metadata/{parent_rating_key}/children"
        response = request_client.get(url, headers=headers, timeout=timeout)
        response.raise_for_status()
        episodes = ET.fromstring(response.content).findall(".//Video")
        episodes_in_season = len(episodes)

        current_episode_number = None
        current_season_number = None
        next_season_number = None
        for episode in episodes:
            if episode.get("ratingKey") == current_episode_rating_key:
                episode_index = episode.get("index")
                if episode_index is None:
                    logger.warning(
                        f"Episode ratingKey {current_episode_rating_key} has no index attribute"
                    )
                    return None
                current_episode_number = int(episode_index)
                current_season_number = episode.get("parentIndex")
                break

        if stay_in_season:
            if current_episode_number == episodes_in_season:
                return None
            for episode in episodes:
                episode_index = episode.get("index")
                if (
                    episode_index is not None
                    and int(episode_index) == int(current_episode_number) + 1
                ):
                    return episode.get("ratingKey")
        else:
            for season in seasons:
                season_index = season.get("index")
                if (
                    season_index is not None
                    and int(season_index) == int(current_season_number) + 1
                ):
                    next_season_number = season.get("ratingKey")
                    break

            if current_episode_number == episodes_in_season:
                if next_season_number is not None:
                    logger.debug("At end of season, try to find next season and first episode.")
                    url = f"{server_ip}/library/metadata/{next_season_number}/children"
                    response = request_client.get(url, headers=headers, timeout=timeout)
                    response.raise_for_status()
                    episodes = ET.fromstring(response.content).findall(".//Video")
                    current_episode_number = 0
                else:
                    return None
            for episode in episodes:
                episode_index = episode.get("index")
                if (
                    episode_index is not None
                    and int(episode_index) == int(current_episode_number) + 1
                ):
                    return episode.get("ratingKey")

        current_file = get_plex_file_name(
            current_episode_rating_key,
            server_ip,
            plex_token,
            timeout=timeout,
            request_client=request_client,
            logger=logger,
        )
        logger.debug(
            f"No next episode found for {current_file}, possibly end of season or series"
        )
        return None
    except request_client.exceptions.RequestException as exc:
        logger.error(f"Error fetching data from Plex: {exc}")
        return None
    except Exception as exc:
        logger.error(f"An unexpected error occurred: {exc}")
        return None


def get_plex_file_name(
    itemid: str,
    server_ip: str,
    plex_token: str,
    *,
    timeout: float,
    request_client,
    logger,
) -> str:
    """Get the full media path for a Plex library item."""
    url = f"{server_ip}/library/metadata/{itemid}"
    headers = {"X-Plex-Token": plex_token}
    response = request_client.get(url, headers=headers, timeout=timeout)

    if response.status_code == 200:
        root = ET.fromstring(response.content)
        part = root.find(".//Part")
        if part is None:
            raise Exception("No Part element found in Plex XML response")
        return part.attrib["file"]
    raise Exception(f"Error: {response.status_code}")


def refresh_plex_metadata(
    itemid: str,
    server_ip: str,
    plex_token: str,
    *,
    timeout: float,
    request_client,
    logger,
) -> None:
    """Request a Plex metadata refresh for a library item."""
    url = f"{server_ip}/library/metadata/{itemid}/refresh"
    headers = {"X-Plex-Token": plex_token}
    response = request_client.put(url, headers=headers, timeout=timeout)

    if response.status_code == 200:
        logger.info("Metadata refresh initiated successfully.")
        return
    raise Exception(f"Error refreshing metadata: {response.status_code}")
