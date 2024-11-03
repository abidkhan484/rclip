import itertools
import os
import re
import sys
import threading
from typing import Iterable, List, NamedTuple, Optional, Tuple, TypedDict, cast

import numpy as np
from tqdm import tqdm
import PIL
from PIL import Image, ImageFile
import rawpy

from rclip import db, fs, model
from rclip.utils.preview import preview
from rclip.utils.snap import check_snap_permissions, is_snap
from rclip.utils import helpers


ImageFile.LOAD_TRUNCATED_IMAGES = True


class ImageMeta(TypedDict):
  modified_at: float
  size: int


PathMetaVector = Tuple[str, ImageMeta, model.FeatureVector]


def get_image_meta(entry: os.DirEntry) -> ImageMeta:
  stat = entry.stat()
  return ImageMeta(modified_at=stat.st_mtime, size=stat.st_size)


def is_image_meta_equal(image: db.Image, meta: ImageMeta) -> bool:
  for key in meta:
    if meta[key] != image[key]:
      return False
  return True


def read_raw_image_file(path: str):
  image = None
  try:
    raw = rawpy.imread(path)
    rgb = raw.postprocess()
    image = Image.fromarray(np.array(rgb))
  except Exception as ex:
    print(f'not a valid raw file {path}', ex, file=sys.stderr)
  return image


def read_image_file(path: str):
  image = None
  try:
    image = Image.open(path)
  except PIL.UnidentifiedImageError as ex:
    print(f'unidentified image error {path}:', ex, file=sys.stderr)
  except Exception as ex:
    print(f'error loading image {path}:', ex, file=sys.stderr)
  return image


def get_file_extension(path: str) -> str:
  return os.path.splitext(path)[1].lower()[1:]


class RClip:
  EXCLUDE_DIRS_DEFAULT = ['@eaDir', 'node_modules', '.git']
  # these images are always processed
  IMAGE_EXT = ["jpg", "jpeg", "png", "webp"]
  # RAW images are processed only if there is no processed image alongside it
  IMAGE_RAW_EXT = ["arw", "dng", "cr2"]
  IMAGE_REGEX = re.compile(f'^.+\\.({"|".join([*IMAGE_EXT, *IMAGE_RAW_EXT])})$', re.I)
  DB_IMAGES_BEFORE_COMMIT = 50_000

  class SearchResult(NamedTuple):
    filepath: str
    score: float

  def __init__(
    self,
    model_instance: model.Model,
    database: db.DB,
    indexing_batch_size: int,
    exclude_dirs: Optional[List[str]],
  ):
    self._model = model_instance
    self._db = database
    self._indexing_batch_size = indexing_batch_size

    excluded_dirs = '|'.join(re.escape(dir) for dir in exclude_dirs or self.EXCLUDE_DIRS_DEFAULT)
    self._exclude_dir_regex = re.compile(f'^.+\\{os.path.sep}({excluded_dirs})(\\{os.path.sep}.+)?$')

  def _index_files(self, filepaths: List[str], metas: List[ImageMeta]):
    images: List[Image.Image] = []
    filtered_paths: List[str] = []
    for path in filepaths:
      file_ext = get_file_extension(path)
      if file_ext in self.IMAGE_EXT:
        image = read_image_file(path)
      elif file_ext in self.IMAGE_RAW_EXT:
        image = read_raw_image_file(path)
      else:
        raise ValueError(f'unsupported image extension: .{file_ext}')

      if image:
        images.append(image)
        filtered_paths.append(path)

    try:
      features = self._model.compute_image_features(images)
    except Exception as ex:
      print('error computing features:', ex, file=sys.stderr)
      return
    for path, meta, vector in cast(Iterable[PathMetaVector], zip(filtered_paths, metas, features)):
      self._db.upsert_image(db.NewImage(
        filepath=path,
        modified_at=meta['modified_at'],
        size=meta['size'],
        vector=vector.tobytes()
      ), commit=False)

  def _does_processed_image_exist_for_raw(self, raw_path: str) -> bool:
    """Check if there is a processed image alongside the raw one; doesn't support mixed-case extensions,
    e.g. it won't detect the .JpG image, but will detect .jpg or .JPG"""

    image_path = os.path.splitext(raw_path)[0]
    for ext in self.IMAGE_EXT:
      if os.path.isfile(image_path + "." + ext):
        return True
      if os.path.isfile(image_path + "." + ext.upper()):
        return True
    return False

  def ensure_index(self, directory: str):
    print(
      'checking images in the current directory for changes;'
      ' use "--no-indexing" to skip this if no images were added, changed, or removed',
      file=sys.stderr,
    )

    self._db.remove_indexing_flag_from_all_images(commit=False)
    self._db.flag_images_in_a_dir_as_indexing(directory, commit=True)

    with tqdm(total=None, unit='images') as pbar:
      def update_total_images(count: int):
        pbar.total = count
        pbar.refresh()
      counter_thread = threading.Thread(
        target=fs.count_files,
        args=(directory, self._exclude_dir_regex, self.IMAGE_REGEX, update_total_images),
      )
      counter_thread.start()

      images_processed = 0
      batch: List[str] = []
      metas: List[ImageMeta] = []
      for entry in fs.walk(directory, self._exclude_dir_regex, self.IMAGE_REGEX):
        filepath = entry.path

        file_ext = get_file_extension(filepath)
        if file_ext in self.IMAGE_RAW_EXT and self._does_processed_image_exist_for_raw(filepath):
          images_processed += 1
          pbar.update()
          continue

        try:
          meta = get_image_meta(entry)
        except Exception as ex:
          print(f'error getting fs metadata for {filepath}:', ex, file=sys.stderr)
          continue

        if not images_processed % self.DB_IMAGES_BEFORE_COMMIT:
          self._db.commit()
        images_processed += 1
        pbar.update()

        image = self._db.get_image(filepath=filepath)
        if image and is_image_meta_equal(image, meta):
          self._db.remove_indexing_flag(filepath, commit=False)
          continue

        batch.append(filepath)
        metas.append(meta)

        if len(batch) >= self._indexing_batch_size:
          self._index_files(batch, metas)
          batch = []
          metas = []

      if len(batch) != 0:
        self._index_files(batch, metas)

      self._db.commit()
      counter_thread.join()

    self._db.flag_indexing_images_in_a_dir_as_deleted(directory)
    print('', file=sys.stderr)

  def search(
      self, query: str, directory: str, top_k: int = 10,
      positive_queries: List[str] = [], negative_queries: List[str] = []) -> List[SearchResult]:
    filepaths, features = self._get_features(directory)

    positive_queries = [query] + positive_queries
    sorted_similarities = self._model.compute_similarities_to_text(features, positive_queries, negative_queries)

    # exclude images that were part of the query from the results
    exclude_files = [
      os.path.abspath(query) for query in positive_queries + negative_queries if helpers.is_file_path(query)
    ]

    filtered_similarities = filter(
      lambda similarity: (
        not self._exclude_dir_regex.match(filepaths[similarity[1]]) and
        not filepaths[similarity[1]] in exclude_files
      ),
      sorted_similarities
    )
    top_k_similarities = itertools.islice(filtered_similarities, top_k)

    return [RClip.SearchResult(filepath=filepaths[th[1]], score=th[0]) for th in top_k_similarities]

  def _get_features(self, directory: str) -> Tuple[List[str], model.FeatureVector]:
    filepaths: List[str] = []
    features: List[model.FeatureVector] = []
    for image in self._db.get_image_vectors_by_dir_path(directory):
      filepaths.append(image['filepath'])
      features.append(np.frombuffer(image['vector'], np.float32))
    if not filepaths:
      return [], np.ndarray(shape=(0, model.Model.VECTOR_SIZE))
    return filepaths, np.stack(features)


def init_rclip(
  working_directory: str,
  indexing_batch_size: int,
  device: str = "cpu",
  exclude_dir: Optional[List[str]] = None,
  no_indexing: bool = False,
):
  datadir = helpers.get_app_datadir()
  db_path = datadir / 'db.sqlite3'

  database = db.DB(db_path)
  model_instance = model.Model(device=device or "cpu")
  rclip = RClip(model_instance, database, indexing_batch_size, exclude_dir)

  if not no_indexing:
    rclip.ensure_index(working_directory)

  return rclip, model_instance, database


def main():
  arg_parser = helpers.init_arg_parser()
  args = arg_parser.parse_args()

  current_directory = os.getcwd()
  if is_snap():
    check_snap_permissions(current_directory)

  rclip, _, db = init_rclip(
    current_directory,
    args.indexing_batch_size,
    vars(args).get("device", "cpu"),
    args.exclude_dir,
    args.no_indexing,
  )

  try:
    result = rclip.search(args.query, current_directory, args.top, args.add, args.subtract)
    if args.filepath_only:
      for r in result:
        print(r.filepath)
    else:
      print('score\tfilepath')
      for r in result:
        print(f'{r.score:.3f}\t"{r.filepath}"')
        if args.preview:
          preview(r.filepath, args.preview_height)
  except Exception as e:
    raise e
  finally:
    db.close()


if __name__ == '__main__':
  main()
