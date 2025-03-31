import faiss
import os
import numpy as np
import time
import argparse
import logging
from typing import Tuple, Optional
import pickle

# Set up logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("faiss_build.log")
    ]
)
logger = logging.getLogger(__name__)

class FAISSBuilder:
    """Builder class for creating and optimizing FAISS indices."""
    
    def __init__(
        self, 
        data_path: str, 
        save_dir: str, 
        index_type: str = "ivfpq",
        use_gpu: bool = True
    ):
        """
        Initialize the FAISS builder.
        
        Args:
            data_path: Path to the embeddings numpy file
            save_dir: Directory to save the index
            index_type: Type of FAISS index to build (ivfpq, hnsw, flat)
            use_gpu: Whether to use GPU if available
        """
        self.data_path = data_path
        self.save_dir = save_dir
        self.index_type = index_type.lower()
        self.use_gpu = use_gpu
        
        # Create save directory if it doesn't exist
        os.makedirs(save_dir, exist_ok=True)

    def load_embeddings(self) -> np.ndarray:
        """
        Load embeddings from file with validation.
        
        Returns:
            Clean embeddings array
        """
        try:
            embeddings = np.load(self.data_path)
            logger.info(f"Loaded {len(embeddings)} embeddings with dimension {embeddings.shape[1]}")
            
            # Check for NaN values
            nan_count = np.isnan(embeddings).sum()
            if nan_count > 0:
                logger.warning(f"Found {nan_count} NaN values in embeddings. Replacing with zeros.")
                embeddings = np.nan_to_num(embeddings)
            
            # Normalize vectors to unit length (for cosine similarity)
            # Uncomment the following lines if you want cosine similarity rather than L2
            # faiss.normalize_L2(embeddings)
            # logger.info("Embeddings normalized to unit length")
            
            return embeddings
            
        except FileNotFoundError:
            logger.error(f"Error: File not found at {self.data_path}")
            raise
        except Exception as e:
            logger.error(f"Error loading embeddings: {str(e)}")
            raise

    def build_index(self, embeddings: np.ndarray) -> faiss.Index:
        """
        Build a FAISS index based on the specified type.
        
        Args:
            embeddings: Matrix of embedding vectors
            
        Returns:
            FAISS index
        """
        dimension = embeddings.shape[1]
        num_vectors = embeddings.shape[0]
        
        # Select index type and configure based on dataset size
        if self.index_type == "flat":
            logger.info("Building Flat index (exact search)")
            index = faiss.IndexFlatL2(dimension)
            
        elif self.index_type == "ivfpq":
            # Scale number of clusters with dataset size
            # Rule of thumb: sqrt(N) clusters where N is dataset size
            num_clusters = min(int(np.sqrt(num_vectors)), 1024)
            num_clusters = max(num_clusters, 4)  # At least 4 clusters
            
            # Scale number of PQ subvectors with dimension
            # Higher dimensions benefit from more subvectors
            num_subvectors = min(64, dimension // 2)
            
            # Bits per subquantizer (usually 8 is good)
            bits_per_code = 8
            
            logger.info(f"Building IVFPQ index with {num_clusters} clusters, "
                      f"{num_subvectors} subvectors, {bits_per_code} bits per code")
            
            quantizer = faiss.IndexFlatL2(dimension)
            index = faiss.IndexIVFPQ(
                quantizer, dimension, num_clusters, 
                num_subvectors, bits_per_code
            )
        
        elif self.index_type == "hnsw":
            # HNSW parameters
            M = 16  # Number of connections per layer (usually 16-64)
            ef_construction = 200  # Higher values = better accuracy but slower build
            
            logger.info(f"Building HNSW index with M={M}, ef_construction={ef_construction}")
            index = faiss.IndexHNSWFlat(dimension, M)
            index.hnsw.efConstruction = ef_construction
            
        else:
            logger.error(f"Unknown index type: {self.index_type}")
            raise ValueError(f"Unknown index type: {self.index_type}")
        
        # Use GPU if requested and available
        if self.use_gpu and faiss.get_num_gpus() > 0:
            logger.info("Moving index to GPU")
            res = faiss.StandardGpuResources()
            index = faiss.index_cpu_to_gpu(res, 0, index)
        
        # Train index if needed
        if hasattr(index, 'train'):
            logger.info("Training index...")
            start_time = time.time()
            index.train(embeddings)
            logger.info(f"Training completed in {time.time() - start_time:.2f} seconds")
        
        # Add vectors to index
        logger.info("Adding vectors to index...")
        start_time = time.time()
        index.add(embeddings)
        logger.info(f"Added {index.ntotal} vectors in {time.time() - start_time:.2f} seconds")
        
        # Set search parameters for better recall
        if hasattr(index, 'nprobe'):
            # Rule of thumb: 10% of clusters but at least 1
            nprobe = max(1, num_clusters // 10)
            index.nprobe = nprobe
            logger.info(f"Search parameter nprobe set to {nprobe}")
        
        if hasattr(index, 'hnsw') and hasattr(index.hnsw, 'efSearch'):
            index.hnsw.efSearch = 64
            logger.info("Search parameter efSearch set to 64")
        
        # Convert back to CPU index for saving
        if self.use_gpu and faiss.get_num_gpus() > 0:
            logger.info("Converting index back to CPU for saving")
            index = faiss.index_gpu_to_cpu(index)
        
        return index

    def test_index(self, index: faiss.Index, embeddings: np.ndarray, k: int = 10) -> Tuple[float, float]:
        """
        Test index quality and performance.
        
        Args:
            index: FAISS index to test
            embeddings: Original embeddings
            k: Number of neighbors to retrieve
            
        Returns:
            Tuple of (avg_time_ms, qps)
        """
        # Sample vectors for testing
        num_test = min(100, len(embeddings))
        test_vectors = embeddings[:num_test].copy()
        
        logger.info(f"Testing search performance with {num_test} queries...")
        
        start_time = time.time()
        for i in range(num_test):
            query = test_vectors[i:i+1]
            _, _ = index.search(query, k)
        end_time = time.time()
        
        total_time = end_time - start_time
        avg_time_ms = (total_time / num_test) * 1000
        qps = num_test / total_time
        
        logger.info(f"Average search time: {avg_time_ms:.2f} ms per query")
        logger.info(f"Queries per second: {qps:.2f} QPS")
        
        return avg_time_ms, qps

    def save_index(self, index: faiss.Index, metadata: Optional[dict] = None) -> str:
        """
        Save the index and metadata.
        
        Args:
            index: FAISS index to save
            metadata: Optional metadata dictionary
            
        Returns:
            Path to saved index
        """
        # Create filename based on index type and date
        timestamp = time.strftime("%Y%m%d-%H%M%S")
        index_filename = f"faiss_{self.index_type}_{timestamp}.index"
        index_path = os.path.join(self.save_dir, index_filename)
        
        # Save index
        faiss.write_index(index, index_path)
        logger.info(f"FAISS index saved to {index_path}")
        
        # Save metadata if provided
        if metadata:
            metadata_path = os.path.join(self.save_dir, f"faiss_{self.index_type}_{timestamp}_meta.pkl")
            with open(metadata_path, 'wb') as f:
                pickle.dump(metadata, f)
            logger.info(f"Index metadata saved to {metadata_path}")
        
        # Create a symlink to latest
        latest_path = os.path.join(self.save_dir, f"faiss_{self.index_type}_latest.index")
        if os.path.exists(latest_path):
            os.remove(latest_path)
        os.symlink(index_path, latest_path)
        logger.info(f"Created symlink to latest index at {latest_path}")
        
        return index_path

    def build(self) -> str:
        """
        Full pipeline to build and save index.
        
        Returns:
            Path to saved index
        """
        build_start = time.time()
        
        # Load data
        embeddings = self.load_embeddings()
        
        # Build index
        index = self.build_index(embeddings)
        
        # Test index performance
        avg_time_ms, qps = self.test_index(index, embeddings)
        
        # Save index with metadata
        metadata = {
            "index_type": self.index_type,
            "num_vectors": len(embeddings),
            "dimension": embeddings.shape[1],
            "build_time": time.time() - build_start,
            "performance": {
                "avg_query_time_ms": avg_time_ms,
                "queries_per_second": qps
            },
            "parameters": {
                "nprobe": index.nprobe if hasattr(index, 'nprobe') else None,
                "ef_search": index.hnsw.efSearch if hasattr(index, 'hnsw') else None
            }
        }
        
        index_path = self.save_index(index, metadata)
        
        logger.info(f"Index build complete in {time.time() - build_start:.2f} seconds")
        
        return index_path


def main():
    # Parse command line arguments
    parser = argparse.ArgumentParser(description="Build a FAISS index for vector search")
    parser.add_argument("--data", required=False, 
                        default=os.path.join(os.path.dirname(os.path.abspath(__file__)), 
                                           "..", "data", "biobert_snomed_embeddings.npy"),
                        help="Path to the embeddings numpy file")
    parser.add_argument("--save-dir", required=False,
                        default=os.path.join(os.path.dirname(os.path.abspath(__file__)), 
                                           "..", "data"),
                        help="Directory to save the index")
    parser.add_argument("--index-type", required=False, default="ivfpq",
                        choices=["flat", "ivfpq", "hnsw"], 
                        help="Type of FAISS index to build")
    parser.add_argument("--no-gpu", action="store_true",
                        help="Disable GPU usage even if available")
    
    args = parser.parse_args()
    
    # Build index
    try:
        builder = FAISSBuilder(
            data_path=args.data,
            save_dir=args.save_dir,
            index_type=args.index_type,
            use_gpu=not args.no_gpu
        )
        
        index_path = builder.build()
        print(f"\nSuccess! FAISS index built and saved to: {index_path}")
        
    except Exception as e:
        logger.error(f"Error building index: {str(e)}")
        import traceback
        logger.error(traceback.format_exc())
        print(f"\nError building index: {str(e)}")
        return 1
    
    return 0

if __name__ == "__main__":
    exit(main())